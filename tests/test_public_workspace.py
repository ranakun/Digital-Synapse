from __future__ import annotations

import pytest
import yaml

import synapse.public_workspace as public_workspace
from synapse.config import resolve_timezone
from synapse.gateway import Gateway
from synapse.host_session import NativeHost
from synapse.public_workspace import initialize_workspace
from synapse.revisions import RevisionStore
from synapse.v2_contracts import V2Error


def test_initialize_workspace_is_empty_source_first_and_resumable(tmp_path):
    result = initialize_workspace(tmp_path / "workspace", timezone="Europe/London")
    root = tmp_path / "workspace"

    assert result == initialize_workspace(root, timezone="UTC")
    assert result["timezone"] == "Europe/London"
    assert RevisionStore(root).head() == result["knowledge_revision"]
    assert RevisionStore(root).manifest()["records"] == {}
    assert not (root / "entities" / "people" / "me.md").exists()
    config = yaml.safe_load((root / ".synapse" / "config.yaml").read_text(encoding="utf-8"))
    assert config == {"vault_path": ".", "workspace": {"timezone": "Europe/London"}}
    assert resolve_timezone(root) == "Europe/London"
    assert resolve_timezone(root, "UTC") == "UTC"
    with pytest.raises(ValueError, match="Unknown timezone"):
        resolve_timezone(root, "Not/A_Timezone")
    assert Gateway(root).view.manifest["records"] == {}


def test_initialize_workspace_rejects_nonempty_unrecognized_destination(tmp_path):
    root = tmp_path / "occupied"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("do not rewrite", encoding="utf-8")

    with pytest.raises(V2Error, match="empty destination"):
        initialize_workspace(root)
    assert marker.read_text(encoding="utf-8") == "do not rewrite"
    assert not (root / "_synapse").exists()


def test_initialize_workspace_resumes_only_its_marked_partial_bootstrap(tmp_path, monkeypatch):
    root = tmp_path / "interrupted"
    with monkeypatch.context() as patched:
        patched.setattr(
            public_workspace.Publisher,
            "bootstrap",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            initialize_workspace(root, timezone="Europe/London")

    assert not (root / "_synapse" / "HEAD").exists()
    result = initialize_workspace(root, timezone="UTC")
    assert result["timezone"] == "Europe/London"
    assert RevisionStore(root).head() == result["knowledge_revision"]
    assert not (root / ".synapse" / public_workspace._MARKER_NAME).exists()

    foreign = tmp_path / "foreign"
    (foreign / ".synapse").mkdir(parents=True)
    (foreign / ".synapse" / public_workspace._MARKER_NAME).write_text(
        '{"format":"public-workspace-initialization/1","timezone":"UTC"}', encoding="utf-8"
    )
    (foreign / "outside.txt").write_text("foreign", encoding="utf-8")
    with pytest.raises(V2Error, match="empty destination"):
        initialize_workspace(foreign)
    assert not (foreign / "_synapse" / "HEAD").exists()


def test_initialize_workspace_resumes_marker_only_interruption(tmp_path, monkeypatch):
    root = tmp_path / "before-publisher"
    with monkeypatch.context() as patched:
        patched.setattr(
            public_workspace,
            "Publisher",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            initialize_workspace(root, timezone="Europe/London")

    assert not (root / "_synapse").exists()
    assert initialize_workspace(root, timezone="UTC")["timezone"] == "Europe/London"

    rejected = tmp_path / "marker-with-foreign-config"
    (rejected / ".synapse").mkdir(parents=True)
    (rejected / ".synapse" / public_workspace._MARKER_NAME).write_text(
        '{"format":"public-workspace-initialization/1","timezone":"UTC"}', encoding="utf-8"
    )
    (rejected / ".synapse" / "foreign.yaml").write_text("foreign: true\n", encoding="utf-8")
    with pytest.raises(V2Error, match="empty destination"):
        initialize_workspace(rejected)


def test_source_capture_does_not_require_a_me_record(tmp_path):
    root = tmp_path / "workspace"
    initialize_workspace(root)
    events = {
        "instruction": {"id": "instruction", "actor": "user", "text": "Save this source."},
        "material": {"id": "material", "actor": "user", "text": "A retained public source."},
    }
    host = NativeHost(root, event_reader=events.__getitem__, display=lambda _value: None, reasoner=None)

    result = host.capture_message("instruction", "material")
    manifest = RevisionStore(root).manifest()
    assert result["capture_receipt"]["knowledge_revision"] == result["knowledge_revision"]
    assert len(manifest["sources"]) == 1
    assert "me" not in manifest["records"]
