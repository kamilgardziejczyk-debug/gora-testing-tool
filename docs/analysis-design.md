# Design: asking Claude about a run's results

Status: **implemented**, as `analysis/analyze.py` and
`scenarios/tracker.analysis.yml`. Section 5 records the development steps and
section 6 what is still unproven.

## 1. Goal

A run leaves an HTML report and up to five logs, and a `!DutStorage`
`copy_from` can pull the DUT's own session data off its SD card. What none of
that gives you is an answer to the questions those artefacts exist to settle:
*why did the GNSS fix drop between sessions*, *does the battery curve explain
the reset*, *is anything anomalous across the whole run*. Those are
correlations across a log and a card's contents, not lines a regex could match.

This is a script that asks them: a YAML file of questions, a list of files, one
request per task, answers in markdown.

## 2. Shape

```bash
python analysis/analyze.py --tasks scenarios/tracker.analysis.yml
python analysis/analyze.py --tasks questions.yml --files results/*.device.log
```

```yaml
tasks:
  - name: "Session gaps"
    files:
      - "results/*.device.log"
      - "results/sd/session_*/*.csv"
    questions:
      - "Correlate GNSS fix loss with power rail dips in the session CSVs."
      - "Does the battery curve explain any reset or gap in the recording?"
```

Two inputs, because they change for different reasons: the questions are
written once and reworded occasionally, the files change every run. `--files`
covers the task that has nothing to say about which evidence it wants, and a
task's own `files:` wins where it has an opinion.

### 2.1 What this replaced, and why

The first version of this design put the questions in the scenario, as a
`!ClaudeAnalyze` tag: the tag recorded tasks during the run, a run record
(`.run.json`) carried them off the node, an `analysis/` package resolved
`inputs:` against a results directory, priced the bundle against a token
ceiling, chose per file between inline and upload, executed against a JSON
schema, drew charts in a server-side sandbox, rendered the answers back into
the HTML report, and could — behind an environment variable — run inline
mid-scenario and assert on the verdict.

Each piece answered a real question. Together they were far more machinery than
"send these files and ask these questions" needs, and most of it existed to
serve the decision to bind a question to a scenario command:

*   Questions in the scenario meant they were **recorded when the scenario
    ran**, so rewording one meant re-flashing the device and sitting through a
    GNSS fix wait to ask it again. The run record, the `inputs:` resolution and
    half the CLI existed to carry a question out of the run it was written in.
*   The **budget packer** priced what the scenario had already committed to
    sending. A person choosing files at the command line prices them by looking
    at the manifest.
*   `mode: assert` and `when: here` bought a gate on a non-reproducible
    answer, at the cost of a credential, an SDK and outbound network **on the
    test bench** — the one place this project works hardest to keep free of
    all three.

What survived is the part that was always doing the work: name the files, ask
the questions, read the answer.

## 3. Decisions that stayed

*   **Nothing runs on the bench.** The scenario writes logs and card copies;
    the questions are asked afterwards, on a developer machine or a second CI
    job. A firmware regression suite must not be able to fail because an API
    call timed out, and `analysis/requirements.txt` stays out of the root
    `requirements.txt` so the node image never carries the SDK.
*   **The evidence is untrusted.** The system prompt says the attached files
    are data and never instructions: firmware can print anything, including
    text shaped like a request.
*   **Nothing is truncated to fit.** A file over `--max-file-mb` stops the run
    with its name and size. A log cut to fit, with nothing saying so, reads
    exactly like a complete one that happened to end there.
*   **A manifest is printed before anything is sent** — every question, every
    file, every size. The last thing printed before money is spent is the list
    of what is being paid for, and `--dry-run` stops there.
*   **A glob matching nothing is a warning, not a failure.** A question about a
    log this run never captured is still worth asking about the logs it did;
    what would not be survivable is not being told.
*   **The credential comes from the environment.** Never a scenario or
    `config.json` — both are committed, and the Dockerfile bakes `scenarios/`
    into the image.

## 4. Transport

Every named file is uploaded through the Files API and referenced as a
`document` block; uploads are cached by path, so evidence shared by three
tasks is uploaded once, and deleted when the answers are in unless
`--keep-uploads` says otherwise. One code path rather than a size-based choice
between inline and upload: an upload is tokenized into context exactly as an
inline file is, so the choice only ever bought request-size head-room, which a
32 MB cap makes moot.

The request is `claude-opus-5`, adaptive thinking, `--effort` (default `high`),
streamed so a long answer cannot hit the SDK's request timeout. A task that
fails is recorded in its place in the markdown, the rest still run, and the
process exits non-zero — the questions are independent, and an answer already
paid for should not be lost to the next one's timeout.

Output is markdown, not JSON against a schema. Nothing downstream reads it: it
is written for a person, and a schema exists to be parsed.

## 5. Development steps

1.  **Strip the previous design.** Revert the `!ClaudeAnalyze` hooks in
    `main.py`, `parser/parser.py` and `wrappers/`; delete the tag, the
    registry, the run record and the `analysis/` package. The deterministic
    report charts (`reporting/charts.py`, the *At a glance* block) stay — they
    are computed from the run's own results and touch no API.
2.  **`analysis/analyze.py`, the offline half** — the tasks file, `--files`,
    glob resolution, the size cap and the `--dry-run` manifest. No SDK import
    on this path, so a glob can be checked on a machine with no credential.
3.  **The API half** — upload, ask, write. Kept in the same file: it is one
    script, and splitting it would mean the seam is the interesting part, which
    it is not.
4.  **Tests, example and docs** — `tests/test_analyze.py` against a fake client
    (the call shape, upload reuse, deletion, per-task failure),
    `scenarios/tracker.analysis.yml`, and the README's Analysis section.

## 6. Open questions

*   **The live API path has never run.** There is no `anthropic` package and no
    credential on the development machine so far. The call shape is tested
    against a fake client and matches the SDK docs, but the first real run is
    untested ground.
*   **`.log` and `.csv` as `text/plain` documents.** Both are uploaded as
    `text/plain`, which the Files API documents for text documents; a card's
    CSV keeps its own extension but not its MIME type.
*   **Cost is bounded by what you name.** `--max-file-mb` caps one file and
    `--effort` caps how hard each task works, but nothing bounds a run ahead of
    time. `--dry-run` plus the printed sizes is the control.
*   **Privacy.** This sends logs and card contents to an external API. Fine for
    firmware output; worth a deliberate decision if a scenario ever captures
    anything that should not leave the bench. There is no redaction step and no
    allow-list of what may be sent.
