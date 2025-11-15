import asyncio
from pathlib import Path

import pytest

from spyoncino.core.contracts import MediaArtifact, ModuleConfig, SnapshotArtifact
from spyoncino.core.orchestrator import Orchestrator
from spyoncino.modules.output.email_notifier import (
    EmailAttachment,
    EmailNotifier,
    EmailSendError,
    SMTPLibEmailSender,
    _ensure_list,
)


class StubEmailSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.sent = asyncio.Event()

    async def send_email(
        self,
        *,
        subject: str,
        body: str,
        sender: str,
        recipients: list[str],
        attachments: list[EmailAttachment] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        attachments = list(attachments or [])
        self.calls.append(
            {
                "subject": subject,
                "body": body,
                "sender": sender,
                "recipients": list(recipients),
                "attachments": attachments,
                "headers": headers or {},
            }
        )
        self.sent.set()


@pytest.mark.asyncio
async def test_email_notifier_delivers_snapshot(tmp_path: Path) -> None:
    artifact_path = tmp_path / "snapshot.png"
    artifact_path.write_bytes(b"fake")

    sender = StubEmailSender()
    notifier = EmailNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "recipients": ["ops@example.com"],
                "from_address": "security@example.com",
                "topic": "event.snapshot.ready",
            }
        ),
    )

    await orchestrator.start()
    artifact = SnapshotArtifact(
        camera_id="lab",
        artifact_path=str(artifact_path),
        metadata={"detection": {"detector_id": "motion", "confidence": 0.7}},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert sender.calls
    call = sender.calls[0]
    assert call["recipients"] == ["ops@example.com"]
    attachments = call["attachments"]
    assert attachments and isinstance(attachments[0], EmailAttachment)
    assert attachments[0].file_path == artifact_path


@pytest.mark.asyncio
async def test_email_notifier_handles_clip(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"clip-bytes")

    sender = StubEmailSender()
    notifier = EmailNotifier(sender=sender)
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "recipients": ["ops@example.com"],
                "clip_topic": "event.clip.ready",
            }
        ),
    )

    await orchestrator.start()
    clip = MediaArtifact(
        camera_id="front",
        artifact_path=str(clip_path),
        media_kind="clip",
        content_type="video/mp4",
        metadata={"detection": {"detector_id": "yolo"}},
    )
    await orchestrator.bus.publish("event.clip.ready", clip)
    await asyncio.wait_for(sender.sent.wait(), timeout=1.0)
    await orchestrator.stop()

    assert sender.calls and sender.calls[0]["attachments"][0].file_path == clip_path


def test_ensure_list_normalizes_inputs() -> None:
    assert _ensure_list(None) == []
    assert _ensure_list("a@example.com, b@example.com") == ["a@example.com", "b@example.com"]
    assert _ensure_list(["c@example.com", ""]) == ["c@example.com"]
    with pytest.raises(ValueError):
        _ensure_list(42)


def test_smtp_sender_validates_options() -> None:
    with pytest.raises(EmailSendError):
        SMTPLibEmailSender(host="", port=25)
    with pytest.raises(EmailSendError):
        SMTPLibEmailSender(host="smtp.local", port=25, use_tls=True, use_ssl=True)


@pytest.mark.asyncio
async def test_email_notifier_configures_smtp_sender(tmp_path: Path) -> None:
    artifact_path = tmp_path / "snap.png"
    artifact_path.write_bytes(b"data")

    notifier = EmailNotifier()
    orchestrator = Orchestrator()

    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "recipients": "ops@example.com",
                "smtp_host": "smtp.local",
                "smtp_port": 2525,
                "topic": "event.snapshot.ready",
            }
        ),
    )
    await orchestrator.start()
    try:
        assert isinstance(notifier._sender, SMTPLibEmailSender)  # type: ignore[attr-defined]
    finally:
        await orchestrator.stop()


@pytest.mark.asyncio
async def test_email_notifier_skips_missing_artifacts(tmp_path: Path) -> None:
    sender = StubEmailSender()
    notifier = EmailNotifier(sender=sender)
    orchestrator = Orchestrator()
    await orchestrator.add_module(
        notifier,
        config=ModuleConfig(
            options={
                "recipients": ["ops@example.com"],
                "topic": "event.snapshot.ready",
            }
        ),
    )
    await orchestrator.start()
    artifact = SnapshotArtifact(
        camera_id="lab",
        artifact_path=str(tmp_path / "missing.png"),
        metadata={},
    )
    await orchestrator.bus.publish("event.snapshot.ready", artifact)
    await asyncio.sleep(0.1)
    await orchestrator.stop()

    assert not sender.calls


def test_render_template_handles_missing_keys() -> None:
    notifier = EmailNotifier()
    notifier._subject_templates["snapshot"] = "Alert {unknown}"  # type: ignore[attr-defined]
    result = notifier._render_subject("snapshot", {}, "garage")  # type: ignore[attr-defined]
    assert result == "Spyoncino alert on garage"
