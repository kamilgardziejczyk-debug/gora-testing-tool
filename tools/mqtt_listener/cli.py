"""Command line front end for the MQTT listener.

Prints every message on a topic until Ctrl-C, or until --duration elapses.
Deciding whether a message is the expected one is left to the caller.
"""

from __future__ import annotations

import argparse
import logging

from .listener import DEFAULT_CONNECT_TIMEOUT_S, DEFAULT_PORT, DEFAULT_QOS, MqttListener

LOGGER = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONNECTION_ERROR = 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Listen to messages published to AWS IoT Core")

    parser.add_argument("--endpoint", required=True, help="Broker host, e.g. xxxx-ats.iot.eu-west-1.amazonaws.com")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Broker port (default: {DEFAULT_PORT})")
    parser.add_argument("--client-id", required=True,
                        help="MQTT client id. Must be permitted by the IoT policy, and must not "
                             "collide with the device's own id or the broker will evict it.")
    parser.add_argument("--cert", required=True, help="Path to the device certificate (PEM)")
    parser.add_argument("--private-key", required=True, help="Path to the private key (PEM)")
    parser.add_argument("--root-ca", required=True, help="Path to the root CA certificate (PEM)")
    parser.add_argument("--topic", required=True, help="Topic filter to subscribe to, '+' and '#' allowed")
    parser.add_argument("--qos", type=int, default=DEFAULT_QOS, choices=(0, 1), help=f"QoS (default: {DEFAULT_QOS})")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT_S,
                        help=f"Seconds to wait for CONNACK (default: {DEFAULT_CONNECT_TIMEOUT_S})")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after this many seconds (default: run until Ctrl-C)")

    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    listener = MqttListener(
        endpoint=args.endpoint,
        port=args.port,
        client_id=args.client_id,
        cert=args.cert,
        private_key=args.private_key,
        root_ca=args.root_ca,
    )

    try:
        listener.connect(timeout_s=args.connect_timeout)
        listener.subscribe(args.topic, qos=args.qos)

        LOGGER.info("Tailing messages, press Ctrl-C to stop")
        for message in listener.stream(args.duration):
            print(f"{message.topic}  {message.payload}", flush=True)
    except ConnectionError as error:
        LOGGER.error("%s", error)
        return EXIT_CONNECTION_ERROR
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
    finally:
        listener.close()

    return EXIT_OK
