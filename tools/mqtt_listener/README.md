# mqtt_listener

Listens to messages published to an MQTT broker over mutual TLS — built for
AWS IoT Core, but works against any broker that speaks MQTT 3.1.1 with client
certificates (a local mosquitto is the easiest way to test).

Usable two ways: as a command line tool that prints what it receives, and as a
Python API that the scenario wrappers drive.

The tool only receives and buffers. Deciding whether a message is the one a
test was waiting for is the caller's job — that comparison lives in the
scenario wrappers, not here.

## Why it buffers

The listener starts buffering the moment `subscribe()` returns, and a reader
drains that buffer whenever it gets round to it. This matters because a device
usually publishes *immediately* after whatever action triggered it. A naive
"connect, then read one message" client loses that race and sees nothing for a
message the device really did send.

So the intended shape of a test is:

1. subscribe (buffering starts)
2. trigger the device
3. read what arrived — whether it landed before or after step 3 began

`subscribe()` waits for the broker's SUBACK, not just for the packet to be
sent, so the subscription is genuinely live before it returns.

## Install

```bash
pip install -r tools/mqtt_listener/requirements.txt   # paho-mqtt>=2.0
```

`paho-mqtt` 2.0 or newer is required — the 1.x client constructor is not
compatible.

## Command line

```bash
python tools/mqtt_listener/mqtt_listener.py \
    --endpoint xxxxxxxxxxxxx-ats.iot.eu-west-1.amazonaws.com \
    --client-id gora-test-listener \
    --cert        certs/device.pem.crt \
    --private-key certs/private.pem.key \
    --root-ca     certs/AmazonRootCA1.pem \
    --topic       'devices/+/telemetry'
```

Prints `<topic>  <payload>` per line until Ctrl-C, or until `--duration`
seconds elapse. Quote the topic — `#` starts a comment in most shells.

```
devices/abc/telemetry  {"status":"ok","t":21}
devices/xyz/telemetry  {"status":"error","code":7}
```

Exit codes: `0` clean exit (duration elapsed or interrupted), `2` connection
failure.

### Options

| Option | Default | Notes |
| --- | --- | --- |
| `--endpoint` | required | Broker hostname |
| `--port` | `8883` | Standard mutual-TLS MQTT port |
| `--client-id` | required | Must be permitted by the IoT policy; see gotchas |
| `--cert` | required | Device certificate (PEM) |
| `--private-key` | required | Private key (PEM) |
| `--root-ca` | required | Root CA certificate (PEM) |
| `--topic` | required | Subscription filter, `+` and `#` allowed |
| `--qos` | `1` | `0` or `1`; IoT Core does not support QoS 2 |
| `--connect-timeout` | `10.0` | Seconds to wait for CONNACK |
| `--duration` | — | Stop after this many seconds (default: until Ctrl-C) |

## Python API

```python
from tools.mqtt_listener import MqttListener

listener = MqttListener(
    endpoint="xxxxxxxxxxxxx-ats.iot.eu-west-1.amazonaws.com",
    port=8883,
    client_id="gora-test-listener",
    cert="certs/device.pem.crt",
    private_key="certs/private.pem.key",
    root_ca="certs/AmazonRootCA1.pem",
)

listener.connect()                                  # raises ConnectionError
listener.subscribe("devices/+/telemetry", qos=1)    # returns once SUBACK arrives

trigger_the_device()                                # messages buffer meanwhile

for message in listener.stream(duration_s=30):      # drains buffer, then waits
    if '"status":"ok"' in message.payload:
        break

listener.close()
```

`MqttListener` is also a context manager, which closes on exit:

```python
with MqttListener(...) as listener:
    listener.connect()
    listener.subscribe("devices/+/telemetry")
    ...
```

### Reference

**`MqttListener(endpoint, port, client_id, cert, private_key, root_ca)`**

| Method | Behaviour |
| --- | --- |
| `connect(timeout_s=10.0)` | TLS connect, block for CONNACK. Raises `ConnectionError` |
| `subscribe(topic, qos=1)` | Subscribe, block for SUBACK. Raises `ConnectionError` |
| `stream(duration_s=None)` | Generator of `Message`; runs forever if `duration_s` is `None` |
| `recent()` | Last 100 messages seen, newest last — for diagnostics |
| `close()` | Disconnect and stop the network thread. Idempotent |

**`Message`** — a `NamedTuple` of `topic` and `payload`, payload decoded as
UTF-8 with `errors="replace"`.

### Semantics worth knowing

- **`stream()` consumes.** Each message is yielded once. Two concurrent
  readers on one listener will each see part of the traffic, not all of it.
- **`recent()` does not consume** — it is a separate 100-message ring kept for
  diagnostics, independent of what `stream()` has taken.
- **The pending buffer holds 1000 messages.** Past that, new messages are
  dropped and a warning is logged once. Only a concern on very chatty topics.
- **`stream(duration_s=N)` bounds total time, not idle time.** It stops N
  seconds after the call, regardless of how many messages arrived.

## Testing against a local mosquitto

No AWS account needed — mosquitto with `require_certificate true` exercises the
identical mutual-TLS path.

Generate a CA and two certificates:

```bash
openssl req -new -x509 -days 365 -nodes -subj "/CN=test-ca" \
        -keyout ca.key -out ca.crt

openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "/CN=localhost" -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days 365 -out server.crt

openssl genrsa -out client.key 2048
openssl req -new -key client.key -subj "/CN=gora-listener" -out client.csr
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days 365 -out client.crt
```

The server certificate's CN **must** match the `--endpoint` you connect to
(`localhost` here) — paho verifies the hostname and will reject a mismatch.

`mosquitto.conf`:

```
listener 8883
cafile   /abs/path/ca.crt
certfile /abs/path/server.crt
keyfile  /abs/path/server.key
require_certificate true
allow_anonymous true
```

Run the broker, start the listener, then publish to it:

```bash
mosquitto -c mosquitto.conf -v

python tools/mqtt_listener/mqtt_listener.py \
    --endpoint localhost --client-id gora-listener \
    --cert client.crt --private-key client.key --root-ca ca.crt \
    --topic 'devices/+/telemetry'

mosquitto_pub -h localhost -p 8883 \
    --cafile ca.crt --cert client.crt --key client.key \
    -t 'devices/abc/telemetry' -m '{"status":"ok"}'
```

`mosquitto_pub` picks a random client ID by default; if you give it one
explicitly, make it different from the listener's, for the reason below.

## AWS IoT Core gotchas

**Client IDs are exclusive.** A broker allows one connection per client ID. If
the listener uses the same ID as the device under test, the two will evict each
other in a loop. Symptom: no messages arrive, with
`connection … dropped unexpectedly` in the log. Give the listener its own ID.

**The IoT policy must permit that ID.** Policies commonly scope `iot:Connect`
to `client/${iot:ClientId}` with a fixed value, and `iot:Subscribe` /
`iot:Receive` to specific topic ARNs. A rejected connect surfaces as
`broker refused the connection for client_id=…`.

**Use the `-ats` endpoint** with `AmazonRootCA1.pem`. The legacy Symantec
endpoint needs a different root CA. `aws iot describe-endpoint
--endpoint-type iot:Data-ATS` prints the right hostname.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `could not establish a TLS connection` | Wrong hostname/port, bad root CA, or server cert CN mismatch |
| `no CONNACK within 10s` | Reachable TCP but no MQTT response — often a firewall or wrong port |
| `broker refused the connection for client_id=…` | IoT policy denies `iot:Connect` for that client ID |
| `connection … dropped unexpectedly` | Another client connected with the same client ID |
| `no SUBACK for '<topic>'` | Policy denies `iot:Subscribe` on that topic |
| Connects fine but nothing prints | Device publishing to a different topic, or the filter does not match |
