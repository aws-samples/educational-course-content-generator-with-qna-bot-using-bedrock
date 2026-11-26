# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for solution.py: the user-agent suffix and the stack description.

Both mechanisms fail silently in production — a client built without the suffix
still works, and a stack deployed with a description lacking the id still
deploys — so the only way to know they are right is to assert on the artifacts
themselves. Here that means the actual outbound ``User-Agent`` header (captured
from a real signed request, intercepted before it leaves the process, so no
credentials and no network are needed) rather than the Config object we passed
in, because botocore is free to rewrite what it is given: the same string
handed to ``user_agent_appid`` instead of ``user_agent_extra`` arrives with its
slashes rewritten to dashes and matches nothing.

Run:  pytest tests/unit/test_solution.py   (or python3 on this file)
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from shipped_files import shipped_files

# The repository root, so that the module is found when this file is run
# directly; pytest already has the root on its path (pytest.ini).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from educational_course_content_generator_with_qna_bot_using_bedrock import (  # noqa: E402
    solution,
)

TOKEN = f"AWSSOLUTION/{solution.SOLUTION_ID}/{solution.SOLUTION_VERSION}"


class _Intercept(Exception):
    """Carries the request headers up out of botocore's send path."""

    def __init__(self, headers):
        self.headers = headers


def _sent_user_agent(client):
    """The User-Agent of a real request from `client`, without sending it."""
    def grab(request, **_kw):
        raise _Intercept(request.headers)

    client.meta.events.register("before-send.*.*", grab)
    try:
        client.get_caller_identity()
    except _Intercept as e:
        ua = e.headers["User-Agent"]
        return ua.decode() if isinstance(ua, bytes) else ua
    raise AssertionError("request was not intercepted; nothing was captured")


def _signed_session():
    """A session with the placeholder credentials that conftest.py puts into the
    environment: enough to sign, nothing is ever sent."""
    import boto3
    return boto3.Session(region_name="us-east-1")


def _sts(**config_kwargs):
    from botocore.config import Config
    config = Config(**config_kwargs) if config_kwargs else None
    return solution.get_client("sts", session=_signed_session(), config=config)


def test_token_reaches_the_wire_verbatim_and_once():
    ua = _sent_user_agent(_sts())
    # whole-token match: the metrics pipeline splits on whitespace, so the
    # token with anything appended to it (TOKEN + 'extra') would not count
    assert ua.split().count(TOKEN) == 1, f"expected {TOKEN!r} once in {ua!r}"
    # botocore's appid path would have rewritten the slashes
    assert TOKEN.replace("/", "-") not in ua, ua


def test_existing_config_survives_the_merge():
    """A caller's timeouts/retries/pool size must not be dropped for the suffix."""
    from botocore.config import Config
    merged = solution.solution_config(
        Config(read_timeout=120, connect_timeout=13,
               retries={"max_attempts": 7}, max_pool_connections=25))
    assert merged.read_timeout == 120, merged.read_timeout
    assert merged.connect_timeout == 13, merged.connect_timeout
    assert merged.retries == {"max_attempts": 7}, merged.retries
    assert merged.max_pool_connections == 25, merged.max_pool_connections

    client = _sts(read_timeout=120, connect_timeout=13,
                  retries={"max_attempts": 7}, max_pool_connections=25)
    cfg = client.meta.config
    assert cfg.read_timeout == 120, cfg.read_timeout
    assert cfg.connect_timeout == 13, cfg.connect_timeout
    # botocore itself restates max_attempts (7 retries) as total_max_attempts
    # (8 tries) when it builds the client; the value survived the merge
    assert cfg.retries["total_max_attempts"] == 8, cfg.retries
    assert cfg.max_pool_connections == 25, cfg.max_pool_connections
    assert _sent_user_agent(client).split().count(TOKEN) == 1


def test_foreign_user_agent_extra_is_concatenated_not_clobbered():
    ua = _sent_user_agent(_sts(user_agent_extra="SomeOtherComponent/1.2.3"))
    fields = ua.split()
    assert "SomeOtherComponent/1.2.3" in fields, ua
    assert fields.count(TOKEN) == 1, ua


def test_applying_the_config_twice_does_not_duplicate_the_token():
    """get_client may be handed a config that already went through here."""
    once = solution.solution_config()
    twice = solution.solution_config(once)
    assert twice.user_agent_extra.split().count(TOKEN) == 1, twice.user_agent_extra


def test_config_may_be_passed_positionally():
    """The Config is the second positional argument for BOTH helpers, and only
    the extra `session` is keyword-only. (Careful: that is not boto3.client's
    own layout — its second positional is region_name.) Locked down because
    call sites are often copied in."""
    from botocore.config import Config
    client = solution.get_client("sts", Config(read_timeout=11),
                                 session=_signed_session())
    assert client.meta.config.read_timeout == 11
    assert _sent_user_agent(client).split().count(TOKEN) == 1
    resource = solution.get_resource("s3", Config(read_timeout=11),
                                     session=_signed_session())
    assert resource.meta.client.meta.config.read_timeout == 11
    assert resource.meta.client.meta.config.user_agent_extra.split().count(
        TOKEN) == 1, resource.meta.client.meta.config.user_agent_extra


def test_a_positional_region_string_fails_at_the_boundary():
    """The classic port-from-boto3.client mistake — boto3.client('sts',
    'us-west-2') copied over — must raise an actionable TypeError at the call
    boundary, not an AttributeError three frames deep in Config.merge."""
    try:
        solution.get_client("sts", "us-west-2", session=_signed_session())
    except TypeError as e:
        assert "region_name" in str(e), e
    else:
        raise AssertionError("a positional region string should have raised")


def test_env_var_overrides_the_config():
    """Code running where the repo is not checked out reads the env var."""
    # deliberately not this solution's real version: proves the env value is
    # used rather than coincidentally matching the config
    override = f"AWSSOLUTION/{solution.SOLUTION_ID}/v9.9.9"
    prev = os.environ.get("USER_AGENT_STRING")
    os.environ["USER_AGENT_STRING"] = override
    try:
        assert solution.user_agent_string() == override
        assert _sent_user_agent(_sts()).split().count(override) == 1
    finally:
        if prev is None:
            del os.environ["USER_AGENT_STRING"]
        else:
            os.environ["USER_AGENT_STRING"] = prev
    assert solution.user_agent_string() == TOKEN


def test_stack_description_shape():
    """The no-component form is what a single-stack app deploys."""
    desc = solution.stack_description()
    assert desc == (f"({solution.SOLUTION_ID}) - {solution.SOLUTION_NAME}. "
                    f"Version {solution.SOLUTION_VERSION}"), desc
    # deployment counting matches on the literal '(SO' prefix
    assert desc.startswith("(SO"), desc
    # no dangling separator or a stray 'None' where the component would go
    assert " -  " not in desc and " - ." not in desc and "None" not in desc, desc
    # '' and whitespace-only mean "no component" — the same bare form, never
    # an empty label with a dangling separator
    assert solution.stack_description("") == desc
    assert solution.stack_description("   ") == desc


def test_stack_description_with_a_component():
    """Still available for telling several stacks of one solution apart."""
    desc = solution.stack_description("test component")
    assert desc == (f"({solution.SOLUTION_ID}) - {solution.SOLUTION_NAME} - "
                    f"test component. Version {solution.SOLUTION_VERSION}"), desc
    assert desc.startswith("(SO"), desc


def test_id_suffix_keeps_one_bare_id_per_install():
    bare = solution.stack_description()
    supporting = solution.stack_description("core", id_suffix="core")
    assert supporting.startswith(f"({solution.SOLUTION_ID}-core)"), supporting
    assert f"({solution.SOLUTION_ID})" not in supporting, supporting
    assert bare.startswith(f"({solution.SOLUTION_ID})"), bare
    # '' included: it must hit the regex and raise — passed through, a
    # supporting stack would silently carry the bare id and double-count.
    # (None is the valid "no suffix" and is covered by `bare` above.)
    for bad in ("has space", "under_score", ""):
        try:
            solution.stack_description("x", id_suffix=bad)
        except ValueError:
            continue
        raise AssertionError(f"id_suffix {bad!r} should have been rejected")


def test_description_over_the_cloudformation_cap_raises():
    """1024 bytes is a hard CloudFormation limit; failing at synth beats failing
    at deploy, and beats silently truncating the version off the end."""
    ok = solution.stack_description("x" * 900)
    assert len(ok.encode("utf-8")) <= 1024
    try:
        solution.stack_description("x" * 1100)
    except ValueError as e:
        assert "1024" in str(e), e
        return
    raise AssertionError("an over-long description should have been rejected")


def test_missing_solution_block_degrades_attribution_but_blocks_a_deploy():
    """The asymmetry that matters: a broken config must not break a running
    pipeline, but must never let a stack deploy uncounted."""
    src = Path(solution.__file__).resolve()
    root = solution.CONFIG_PATH.parent
    # reproduce this repo's layout inside the temp dir, whatever it is: the
    # module at its real depth below the root, the config file at the root
    rel = src.parent.relative_to(root)
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / rel
        pkg.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, pkg / "solution.py")
        # every other block a real config carries, but no 'solution' — which
        # is what a user's own edited config looks like
        (Path(tmp) / solution.CONFIG_FILE).write_text(
            json.dumps({"some_other_block": {"unrelated": True}}))
        # A second copy of the module, loaded under its own name: it reads the
        # config file next to it, which has no block. Without the environment
        # variable, which would stand in for the missing block.
        spec = importlib.util.spec_from_file_location(
            "solution_without_block", pkg / "solution.py")
        without_block = importlib.util.module_from_spec(spec)
        previous = os.environ.pop("USER_AGENT_STRING", None)
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                spec.loader.exec_module(without_block)
                assert without_block.user_agent_string() == "", (
                    without_block.user_agent_string())
                from botocore.config import Config
                # `is None`, not just falsy: with no identity, nothing at all
                # is merged over the caller's config (an empty-string override
                # would also pass a bare falsiness check)
                assert without_block.solution_config().user_agent_extra is None
                # and a caller's own suffix + options survive the no-identity
                # path
                cfg = without_block.solution_config(
                    Config(user_agent_extra="Foreign/1.0", read_timeout=42))
                assert cfg.user_agent_extra == "Foreign/1.0", cfg.user_agent_extra
                assert cfg.read_timeout == 42, cfg.read_timeout
                try:
                    without_block.stack_description("c")
                except ValueError:
                    pass
                else:
                    raise AssertionError("stack_description must refuse to build "
                                         "an uncountable description")
        finally:
            if previous is not None:
                os.environ["USER_AGENT_STRING"] = previous
        assert "no 'solution' block" in stderr.getvalue(), stderr.getvalue()


def test_the_identity_path_is_fixed():
    """The identity must not travel with a per-run config override: it is
    release metadata, and an override that omitted the block would leave runs
    unattributed or block a deploy."""
    assert solution.CONFIG_PATH.name == solution.CONFIG_FILE
    assert solution.CONFIG_PATH.is_absolute(), solution.CONFIG_PATH
    assert solution.identity()["id"] == solution.SOLUTION_ID
    # The stacks read project_config.json from the current directory (the
    # Classic mode tests synthesize from a temporary directory with a patched
    # copy), but the identity always comes from the file of this repository.
    assert solution.CONFIG_PATH == REPO_ROOT / solution.CONFIG_FILE


def test_config_is_the_only_place_the_values_are_written():
    """A second literal of these values would go stale at the next release.

    Prose may restate the *format* (that is what the READMEs do, with
    placeholders) — what must not be duplicated anywhere is the id, the version,
    or a built ``AWSSOLUTION/`` token. The version is the one that drifts, so it
    is checked in documentation too.
    """
    data = json.loads(solution.CONFIG_PATH.read_text())["solution"]
    assert data["id"] == solution.SOLUTION_ID
    assert data["name"] == solution.SOLUTION_NAME
    assert data["version"] == solution.SOLUTION_VERSION
    repo = solution.CONFIG_PATH.parent
    # Every name in this set is a place a stale value can hide, so keep it
    # minimal.
    skip = {solution.CONFIG_FILE, "solution.py", "test_solution.py"}
    # the files that .gitignore does not exclude, so ignored paths (local
    # scratch, generated config) are out of scope: this asserts about what
    # ships, not what is lying around
    offenders = []
    for rel in shipped_files():
        path = repo / rel
        if path.name in skip:
            continue
        if path.suffix in {".py", ".json", ".sh", ".yaml", ".yml", ".ts",
                           ".toml", ".ini", ".cfg", ".txt"}:
            banned = (solution.SOLUTION_ID, solution.SOLUTION_VERSION,
                      "AWSSOLUTION/")
        elif path.suffix == ".md":
            banned = (solution.SOLUTION_VERSION,)
        else:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        hits = [b for b in banned if b in text]
        if hits:
            offenders.append(f"{rel} {hits}")
    assert not offenders, (
        f"solution id / version / user-agent token hardcoded outside "
        f"{solution.CONFIG_FILE} and its one reader: {offenders}")


if __name__ == "__main__":
    # what conftest.py does for pytest: placeholder credentials to sign with,
    # and none of the AWS settings of this machine
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        os.environ[name] = "testing"
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        os.environ.pop(name, None)
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        os.environ[name] = os.devnull
    for fn in (test_token_reaches_the_wire_verbatim_and_once,
               test_existing_config_survives_the_merge,
               test_foreign_user_agent_extra_is_concatenated_not_clobbered,
               test_applying_the_config_twice_does_not_duplicate_the_token,
               test_config_may_be_passed_positionally,
               test_a_positional_region_string_fails_at_the_boundary,
               test_env_var_overrides_the_config,
               test_stack_description_shape,
               test_stack_description_with_a_component,
               test_id_suffix_keeps_one_bare_id_per_install,
               test_description_over_the_cloudformation_cap_raises,
               test_missing_solution_block_degrades_attribution_but_blocks_a_deploy,
               test_the_identity_path_is_fixed,
               test_config_is_the_only_place_the_values_are_written):
        fn()
        print(f"PASS {fn.__name__}")
    print("all solution tests passed")
