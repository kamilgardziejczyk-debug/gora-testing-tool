"""Tests for `analysis/analyze.py`.

The API half is exercised against a fake client that records the call shape,
so the whole file is testable with neither the SDK nor a credential - the same
property that lets `--dry-run` work on a machine that has neither.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import analyze  # noqa: E402
from analyze import Answer, AnalyzeError, Task  # noqa: E402


class FakeUpload:
    """What `files.upload` returns: an id and nothing else this code reads."""

    def __init__(self, file_id: str) -> None:
        self.id = file_id


class FakeStream:
    """A context manager standing in for the SDK's streaming response."""

    def __init__(self, message: object) -> None:
        self._message = message

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_final_message(self) -> object:
        return self._message


class FakeBlock:
    def __init__(self, text: str, block_type: str = "text") -> None:
        self.text = text
        self.type = block_type


class FakeOutput:
    """One file the sandbox produced, as a code execution result carries it."""

    def __init__(self, file_id: str, filename: str) -> None:
        self.type = "bash_code_execution_output"
        self.file_id = file_id
        self.filename = filename


class FakeToolResult:
    """A `bash_code_execution_tool_result` block wrapping those outputs."""

    def __init__(self, outputs: list) -> None:
        self.type = "bash_code_execution_tool_result"
        self.content = type("Result", (), {"content": outputs})()


class FakeDownload:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def write_to_file(self, path: str) -> None:
        Path(path).write_bytes(self.payload)


class FakeMessage:
    def __init__(self, blocks: list, stop_reason: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


class FakeClient:
    """Records every call, answers with canned text, and can be told to fail."""

    def __init__(
        self,
        answer: str = "An answer.",
        fail_on: str | None = None,
        outputs: list | None = None,
        pauses: int = 0,
    ) -> None:
        self.answer = answer
        self.fail_on = fail_on
        self.outputs = outputs or []
        self.pauses = pauses
        self.uploads: list[str] = []
        self.deleted: list[str] = []
        self.downloaded: list[str] = []
        self.requests: list[dict] = []
        self.beta = self

        class Files:
            def __init__(self, outer: "FakeClient") -> None:
                self.outer = outer

            def upload(self, file):  # noqa: ANN001
                name = file[0]
                self.outer.uploads.append(name)
                return FakeUpload(f"file_{len(self.outer.uploads)}")

            def delete(self, file_id):  # noqa: ANN001
                self.outer.deleted.append(file_id)

            def download(self, file_id):  # noqa: ANN001
                self.outer.downloaded.append(file_id)
                return FakeDownload(b"\x89PNG")

        class Messages:
            def __init__(self, outer: "FakeClient") -> None:
                self.outer = outer

            def stream(self, **kwargs):  # noqa: ANN003
                self.outer.requests.append(kwargs)
                text = kwargs["messages"][0]["content"][-1]["text"]
                if self.outer.fail_on and self.outer.fail_on in text:
                    raise RuntimeError("the API said no")
                if len(self.outer.requests) <= self.outer.pauses:
                    paused = FakeMessage([FakeBlock("Drawing...")], stop_reason="pause_turn")
                    return FakeStream(paused)
                blocks = [FakeBlock("ignored", "thinking")]
                if "tools" in kwargs:
                    # With the sandbox on, Claude narrates before it answers.
                    blocks.append(FakeBlock("Let me plot that."))
                    blocks.append(FakeToolResult(self.outer.outputs))
                blocks.append(FakeBlock(self.outer.answer))
                return FakeStream(FakeMessage(blocks))

        self.files = Files(self)
        self.messages = Messages(self)


class TasksFileTests(unittest.TestCase):
    """Reading the YAML: what is accepted, and what is refused and why."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, text: str) -> Path:
        path = self.root / "tasks.yml"
        path.write_text(text)
        return path

    def touch(self, name: str, size: int = 16) -> Path:
        path = self.root / name
        path.write_text("x" * size)
        return path

    def test_reads_name_questions_and_files(self) -> None:
        self.touch("run.device.log")
        path = self.write(
            "tasks:\n"
            "  - name: Session gaps\n"
            f"    files: ['{self.root}/*.device.log']\n"
            "    questions:\n"
            "      - Why did the fix drop?\n"
            "      - Does the battery curve explain it?\n"
        )
        tasks = analyze.load_tasks(path, [], analyze.DEFAULT_MAX_FILE_MB)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].name, "Session gaps")
        self.assertEqual(len(tasks[0].questions), 2)
        self.assertEqual([p.name for p in tasks[0].files], ["run.device.log"])

    def test_single_question_may_be_a_string(self) -> None:
        path = self.write("tasks:\n  - questions: Why did it reset?\n")
        tasks = analyze.load_tasks(path, [], analyze.DEFAULT_MAX_FILE_MB)
        self.assertEqual(tasks[0].questions, ["Why did it reset?"])

    def test_task_without_files_falls_back_to_the_command_line(self) -> None:
        self.touch("a.log")
        path = self.write('tasks:\n  - questions: ["Anything odd?"]\n')
        tasks = analyze.load_tasks(path, [f"{self.root}/a.log"], analyze.DEFAULT_MAX_FILE_MB)
        self.assertEqual([p.name for p in tasks[0].files], ["a.log"])

    def test_declared_files_win_over_the_command_line(self) -> None:
        self.touch("a.log")
        self.touch("b.log")
        path = self.write(
            f"tasks:\n  - files: ['{self.root}/b.log']\n    questions: ['Anything odd?']\n"
        )
        tasks = analyze.load_tasks(path, [f"{self.root}/a.log"], analyze.DEFAULT_MAX_FILE_MB)
        self.assertEqual([p.name for p in tasks[0].files], ["b.log"])

    def test_file_without_tasks_is_refused(self) -> None:
        path = self.write("questions: [Why?]\n")
        with self.assertRaises(AnalyzeError):
            analyze.load_tasks(path, [], analyze.DEFAULT_MAX_FILE_MB)

    def test_task_without_questions_is_refused(self) -> None:
        path = self.write("tasks:\n  - name: Nothing asked\n")
        with self.assertRaises(AnalyzeError):
            analyze.load_tasks(path, [], analyze.DEFAULT_MAX_FILE_MB)

    def test_missing_file_is_refused_by_path(self) -> None:
        with self.assertRaises(AnalyzeError) as caught:
            analyze.load_tasks(self.root / "absent.yml", [], analyze.DEFAULT_MAX_FILE_MB)
        self.assertIn("absent.yml", str(caught.exception))


class ResolveFilesTests(unittest.TestCase):
    """Which files a pattern names, and what happens when it names none."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_globs_expand_sorted_and_deduplicated(self) -> None:
        for name in ("b.log", "a.log"):
            (self.root / name).write_text("x")
        patterns = [f"{self.root}/*.log", f"{self.root}/a.log"]
        resolved = analyze.resolve_files(patterns, analyze.DEFAULT_MAX_FILE_MB)
        self.assertEqual([p.name for p in resolved], ["a.log", "b.log"])

    def test_declared_order_is_kept_across_patterns(self) -> None:
        for name in ("z.csv", "a.log"):
            (self.root / name).write_text("x")
        resolved = analyze.resolve_files(
            [f"{self.root}/z.csv", f"{self.root}/a.log"], analyze.DEFAULT_MAX_FILE_MB
        )
        self.assertEqual([p.name for p in resolved], ["z.csv", "a.log"])

    def test_pattern_matching_nothing_is_skipped_not_fatal(self) -> None:
        (self.root / "a.log").write_text("x")
        with self.assertLogs(analyze.LOGGER, level="WARNING"):
            resolved = analyze.resolve_files(
                [f"{self.root}/none.mqtt.log", f"{self.root}/a.log"],
                analyze.DEFAULT_MAX_FILE_MB,
            )
        self.assertEqual([p.name for p in resolved], ["a.log"])

    def test_directory_is_not_evidence(self) -> None:
        (self.root / "sd").mkdir()
        self.assertEqual(analyze.resolve_files([f"{self.root}/*"], 32), [])

    def test_oversized_file_stops_the_run(self) -> None:
        (self.root / "huge.log").write_text("x" * 4096)
        with self.assertRaises(AnalyzeError) as caught:
            analyze.resolve_files([f"{self.root}/huge.log"], max_file_mb=0.001)
        self.assertIn("huge.log", str(caught.exception))


class RequestShapeTests(unittest.TestCase):
    """What is actually sent, and what is done with what comes back."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.log = self.root / "run.device.log"
        self.log.write_text("boot\n")

    def task(self, name: str = "Gaps", questions: list | None = None) -> Task:
        return Task(name=name, questions=questions or ["Why?"], files=[self.log])

    def test_files_travel_as_documents_and_questions_as_text(self) -> None:
        client = FakeClient()
        answers, failed = analyze.run_tasks(client, [self.task()], analyze.Options())

        self.assertEqual(failed, 0)
        self.assertEqual([answer.text for answer in answers], ["An answer."])
        request = client.requests[0]
        content = request["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"], {"type": "file", "file_id": "file_1"})
        self.assertEqual(content[-1]["type"], "text")
        self.assertIn("Why?", content[-1]["text"])
        self.assertEqual(request["model"], "claude-opus-5")
        self.assertEqual(request["output_config"], {"effort": "high"})
        self.assertEqual(request["thinking"], {"type": "adaptive"})
        self.assertIn(analyze.FILES_BETA, request["betas"])

    def test_only_text_blocks_become_the_answer(self) -> None:
        client = FakeClient(answer="The fix dropped at 12:03.")
        answers, _ = analyze.run_tasks(client, [self.task()], analyze.Options())
        self.assertEqual(answers[0].text, "The fix dropped at 12:03.")

    def test_shared_evidence_is_uploaded_once(self) -> None:
        client = FakeClient()
        tasks = [self.task("First"), self.task("Second")]
        analyze.run_tasks(client, tasks, analyze.Options())

        self.assertEqual(client.uploads, ["run.device.log"])
        self.assertEqual(len(client.requests), 2)

    def test_uploads_are_deleted_afterwards(self) -> None:
        client = FakeClient()
        analyze.run_tasks(client, [self.task()], analyze.Options())
        self.assertEqual(client.deleted, ["file_1"])

    def test_keep_uploads_leaves_them_on_the_account(self) -> None:
        client = FakeClient()
        analyze.run_tasks(client, [self.task()], analyze.Options(keep_uploads=True))
        self.assertEqual(client.deleted, [])

    def test_one_task_failing_does_not_stop_the_others(self) -> None:
        client = FakeClient(fail_on="Explode")
        tasks = [self.task("Bad", ["Explode please"]), self.task("Good", ["Why?"])]
        with self.assertLogs(analyze.LOGGER, level="ERROR"):
            answers, failed = analyze.run_tasks(client, tasks, analyze.Options())

        self.assertEqual(failed, 1)
        self.assertIn("Failed:", answers[0].text)
        self.assertEqual(answers[1].text, "An answer.")
        self.assertEqual(client.deleted, ["file_1"])


class OutputTests(unittest.TestCase):
    """The manifest printed before anything is sent, and the markdown written after."""

    def test_manifest_names_every_question_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            log.write_text("x")
            task = Task(name="Gaps", questions=["Why?"], files=[log])
            manifest = analyze.format_manifest([task], analyze.Options())

        self.assertIn("Gaps", manifest)
        self.assertIn("Why?", manifest)
        self.assertIn("a.log", manifest)

    def test_manifest_flags_a_task_with_no_evidence(self) -> None:
        task = Task(name="Gaps", questions=["Why?"], files=[])
        self.assertIn("no files", analyze.format_manifest([task], analyze.Options()))

    def test_answers_are_written_under_their_task_name(self) -> None:
        task = Task(name="Session gaps", questions=["Why?"], files=[])
        document = analyze.format_answers([task], [Answer("Because.")], analyze.Options())

        self.assertIn("## Session gaps", document)
        self.assertIn("Because.", document)
        self.assertIn("claude-opus-5", document)


if __name__ == "__main__":
    unittest.main()
