# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How the solution identity reaches what this repository deploys and runs.

test_solution.py covers the reader (solution.py). These tests cover its users, and they read the
artifacts instead of the wiring: the templates that ``app.py`` synthesizes, as ``cdk synth`` runs it,
and the ``User-Agent`` header of real requests that are intercepted before they are sent. Every AWS SDK
client that the code under lambda/ creates has such a header test, here or in test_agent_handler.py. Two
tests read the source as well: they are the net for a client that is added later without one.

``app.py`` runs from the repository root and, for Classic mode, from a temporary directory with a patched
copy of project_config.json, like the Classic mode tests in test_stacks.py. Asset bundling is disabled, so
no Docker is needed.
"""
import ast
import functools
import importlib.util
import json
import logging
import os
import runpy
import shutil
import sys
import types
from pathlib import Path

import aws_cdk as cdk
import boto3
import botocore.session
import pytest
from shipped_files import shipped_files

from educational_course_content_generator_with_qna_bot_using_bedrock import solution

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that the stacks package as assets, relative to the directory they are synthesized from
ASSET_DIRECTORIES = ("lambda", "kb_dataset")

# CloudFormation limit for a template description
DESCRIPTION_MAX_BYTES = 1024


# A value of the USER_AGENT_STRING variable in the shell of whoever deploys. It is not the token of this
# solution, and app.py must not pass it on to what it deploys.
FOREIGN_USER_AGENT = "another-tool/1.0"


def run_app(working_directory, out_directory):
    """Synthesize the application with app.py, the entry point that cdk.json names, in ``working_directory``.

    ``cdk synth`` passes the output directory and the context of cdk.json to the application through the
    environment of a new process. Here app.py runs in this process, where the CDK has already started with
    another environment, so its ``cdk.App()`` call gets them as arguments instead. The stacks name no account
    and no Region, so the templates do not depend on the machine.
    """
    with open(REPO_ROOT / "cdk.json", encoding="utf-8") as cdk_json:
        context = {**json.load(cdk_json)["context"], "aws:cdk:bundling-stacks": []}
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(working_directory)
        patch.setattr(cdk, "App", functools.partial(cdk.App, outdir=str(out_directory), context=context))
        patch.setenv("USER_AGENT_STRING", FOREIGN_USER_AGENT)
        patch.setattr(sys, "dont_write_bytecode", True)
        runpy.run_path(str(REPO_ROOT / "app.py"), run_name="__main__")


@pytest.fixture(scope="module")
def project_config():
    with open(REPO_ROOT / "project_config.json", encoding="utf-8") as config_file:
        return json.load(config_file)


@pytest.fixture(scope="module", params=["configured", "classic"])
def cloud_assembly(request, project_config, tmp_path_factory):
    """The synthesized application in the mode of project_config.json and in Classic mode: the three
    templates by stack name, the stacks each stack depends on, the mode, and the output directory, which
    holds a copy of the code of every Lambda function."""
    out_directory = tmp_path_factory.mktemp(f"cdk_out_{request.param}")
    if request.param == "configured":
        run_app(REPO_ROOT, out_directory)
    else:
        working_directory = tmp_path_factory.mktemp("classic_mode_app")
        classic_config = {**project_config, "qna_bot_mode": "classic"}
        (working_directory / "project_config.json").write_text(json.dumps(classic_config), encoding="utf-8")
        for name in ASSET_DIRECTORIES:
            try:
                (working_directory / name).symlink_to(REPO_ROOT / name, target_is_directory=True)
            except OSError:  # Creating symbolic links can require extra privileges on Windows
                shutil.copytree(REPO_ROOT / name, working_directory / name)
        run_app(working_directory, out_directory)

    manifest = json.loads((out_directory / "manifest.json").read_text(encoding="utf-8"))
    stacks = {name: artifact for name, artifact in manifest["artifacts"].items()
              if artifact["type"] == "aws:cloudformation:stack"}
    return {
        "mode": project_config["qna_bot_mode"] if request.param == "configured" else "classic",
        "out_directory": out_directory,
        "templates": {name: json.loads((out_directory / f"{name}.template.json").read_text(encoding="utf-8"))
                      for name in stacks},
        "dependencies": {name: {dependency for dependency in artifact.get("dependencies", [])
                                if dependency in stacks}
                         for name, artifact in stacks.items()},
    }


# --- Deployment counting: the stack descriptions ---------------------------------------------------

def test_every_stack_description_carries_the_solution_id_name_and_version(cloud_assembly, project_config):
    templates = cloud_assembly["templates"]
    assert set(templates) == {project_config[key] for key in
                              ("CloudFrontWAFStackName", "CourseStackName", "QnAStackName")}
    for name, template in templates.items():
        description = template.get("Description", "")
        assert description.startswith(f"({solution.SOLUTION_ID}"), f"{name}: {description!r}"
        assert solution.SOLUTION_NAME in description, f"{name}: {description!r}"
        assert description.endswith(f". Version {solution.SOLUTION_VERSION}"), f"{name}: {description!r}"
        assert len(description.encode("utf-8")) <= DESCRIPTION_MAX_BYTES, f"{name}: {description!r}"


def test_one_installation_is_counted_once(cloud_assembly, project_config):
    """Deployment counting matches the id in parentheses. One installation deploys three stacks, so exactly
    one of them carries the bare id and the others carry the id with a suffix."""
    bare = f"({solution.SOLUTION_ID})"
    descriptions = {name: template["Description"] for name, template in cloud_assembly["templates"].items()}
    counted = [name for name, description in descriptions.items() if description.startswith(bare)]
    assert counted == [project_config["QnAStackName"]], descriptions
    for name, description in descriptions.items():
        if name not in counted:
            assert description.startswith(f"({solution.SOLUTION_ID}-"), f"{name}: {description!r}"
            assert bare not in description, f"{name}: {description!r}"


def test_the_counted_stack_is_the_one_that_depends_on_the_others(cloud_assembly, project_config):
    """A stack that others depend on can be left behind by a deployment that was abandoned half-way, so the
    stack that is deployed last carries the bare id: it depends, directly or through another stack, on every
    other stack of the application, and no stack depends on it."""
    dependencies = cloud_assembly["dependencies"]
    counted = project_config["QnAStackName"]

    def reachable(name, seen=None):
        seen = set() if seen is None else seen
        for dependency in dependencies[name]:
            if dependency not in seen:
                seen.add(dependency)
                reachable(dependency, seen)
        return seen

    assert reachable(counted) == set(dependencies) - {counted}, dependencies
    assert not [name for name, needs in dependencies.items() if counted in needs], dependencies


# --- API usage attribution: the Lambda functions and the agent -------------------------------------
#
# Their code runs without the repository, so it cannot import solution.py. The stacks pass the token in the
# USER_AGENT_STRING environment variable, and the code gives it to every AWS SDK client it creates.

# The Lambda functions that the CDK adds for custom resources, by the start of their logical ID. They run
# code of the CDK. Every other Lambda function is a function of this repository.
CDK_FUNCTIONS = (
    "CustomVpcRestrictDefaultSGCustomResourceProviderHandler",
    "CustomS3AutoDeleteObjectsCustomResourceProviderHandler",
    "CustomCDKBucketDeployment",
    "CustomResourceIndexCreatorframeworkonEvent",
    "BucketNotificationsHandler",
)

LAMBDA_SOURCES = sorted(path for path in (REPO_ROOT / "lambda").rglob("*.py") if "__pycache__" not in path.parts)

# What a detached file passes as user_agent_extra, as ast.unparse writes it
USER_AGENT_FROM_ENVIRONMENT = "os.environ.get('USER_AGENT_STRING', '')"

# The methods that create an AWS SDK client
CLIENT_FACTORIES = ("client", "resource", "create_client")


def _creates_sdk_clients(code_directory):
    return any(isinstance(call.func, ast.Attribute) and call.func.attr in CLIENT_FACTORIES
               for path in sorted(code_directory.rglob("*.py")) for call in _calls(path)[0])


def test_every_lambda_function_that_calls_aws_receives_the_token(cloud_assembly, project_config):
    """Every Lambda function of this repository whose code creates an AWS SDK client must get the token, and
    it must be the token of project_config.json, whatever USER_AGENT_STRING holds in the shell that deploys.
    A function that is left out works, and its calls are never attributed: nothing fails.

    The code of a function is read from the copy that the synthesis makes of it. A function whose code is
    not there (inline code, a container image) must carry the token as well, so that a new function cannot
    pass this test by being of a kind that the test does not know. The functions that make no AWS SDK call
    (the authorizers, the index creator) and the functions of the CDK get no variable that they never read.
    """
    token = solution.user_agent_string()
    assert token.count("/") == 2 and token != FOREIGN_USER_AGENT, token
    with_token, without_token = {}, {}
    for name, template in cloud_assembly["templates"].items():
        for logical_id, resource in template.get("Resources", {}).items():
            if resource["Type"] != "AWS::Lambda::Function":
                continue
            properties = resource["Properties"]
            variables = properties.get("Environment", {}).get("Variables", {})
            where = f"{name}/{logical_id}"
            if logical_id.startswith(CDK_FUNCTIONS):
                assert "USER_AGENT_STRING" not in variables, where
                continue
            asset_key = str(properties.get("Code", {}).get("S3Key", ""))
            code_directory = cloud_assembly["out_directory"] / f"asset.{asset_key.split('.')[0]}"
            if code_directory.is_dir() and not _creates_sdk_clients(code_directory):
                assert "USER_AGENT_STRING" not in variables, f"{where} never reads the variable: {variables}"
                without_token.setdefault(name, []).append(logical_id)
            else:
                assert variables.get("USER_AGENT_STRING") == token, f"{where}: {variables}"
                with_token.setdefault(name, []).append(logical_id)
    # Both stacks that hold Lambda functions were looked at, and both kinds of function were found
    stacks_with_functions = {project_config["CourseStackName"], project_config["QnAStackName"]}
    assert set(with_token) == set(without_token) == stacks_with_functions, (with_token, without_token)


def test_the_agent_runtime_receives_the_token(cloud_assembly, project_config):
    runtimes = [resource
                for template in cloud_assembly["templates"].values()
                for resource in template.get("Resources", {}).values()
                if resource["Type"] == "AWS::BedrockAgentCore::Runtime"]
    for runtime in runtimes:
        variables = runtime["Properties"]["EnvironmentVariables"]
        assert variables.get("USER_AGENT_STRING") == solution.user_agent_string(), variables
    # Strands Agent mode has one runtime; Classic mode has none
    assert len(runtimes) == (1 if cloud_assembly["mode"] == "strands" else 0), cloud_assembly["mode"]


def _calls(path):
    """The calls of a Python file, and the expressions that its module-level names are bound to."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_constants = {target.id: node.value
                        for node in tree.body if isinstance(node, ast.Assign)
                        for target in node.targets if isinstance(target, ast.Name)}
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)], module_constants


def _from_imports(path):
    """What the ``from`` imports of a Python file bind: local name -> (module, imported name)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {alias.asname or alias.name: (node.module, alias.name)
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names}


def _keyword(call, name):
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


# The modules that botocore's Config is imported from
CONFIG_MODULES = ("botocore.config", "botocore.client")


def _is_config_call(call, imported):
    """Whether a call creates a botocore ``Config``: under its own name, under the alias that the file imports
    it with, or as an attribute (``botocore.config.Config``)."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr == "Config"
    name = getattr(call.func, "id", "")
    return name == "Config" or imported.get(name) in [(module, "Config") for module in CONFIG_MODULES]


def _module_level_value(path, name, module_constants, imported):
    """The expression that a module-level name is bound to: in the file itself, or in the module next to it
    that the file imports the name from (the handlers of the generation functions take SDK_CONFIG from their
    helper module). None when the source does not show it."""
    if name in module_constants:
        return module_constants[name]
    module, imported_name = imported.get(name, (None, None))
    neighbour = path.parent / f"{module}.py"
    if module and neighbour.is_file():
        return _calls(neighbour)[1].get(imported_name)
    return None


def test_lambda_sources_give_every_sdk_client_the_user_agent_suffix():
    """A client that is created without the suffix works, and its calls are never attributed: nothing fails.
    The header tests below cover the clients that exist; this is the net for a client that is added later.
    Every client under lambda/ must be created with a ``config``, and every botocore ``Config`` there must
    take its ``user_agent_extra`` from the environment variable."""
    problems = []
    clients = 0
    for path in LAMBDA_SOURCES:
        relative_path = path.relative_to(REPO_ROOT)
        calls, module_constants = _calls(path)
        imported = _from_imports(path)
        for local_name, (module, name) in imported.items():
            if module == "boto3" and name in CLIENT_FACTORIES:
                problems.append(f"{relative_path}: 'from boto3 import {name}' hides the clients that "
                                f"{local_name}() creates from this test; write boto3.{name}()")
        for call in calls:
            where = f"{relative_path}:{call.lineno}"
            if isinstance(call.func, ast.Attribute) and call.func.attr in CLIENT_FACTORIES:
                clients += 1
                config = _keyword(call, "config")
                if isinstance(config, ast.Name):
                    # A module-level name stands for the expression that it is bound to
                    config = _module_level_value(path, config.id, module_constants, imported) or config
                if config is None:
                    problems.append(f"{where}: {ast.unparse(call.func)}() without config=")
                elif isinstance(config, ast.Constant):
                    problems.append(f"{where}: {ast.unparse(call.func)}() with config={ast.unparse(config)}")
            elif _is_config_call(call, imported):
                suffix = _keyword(call, "user_agent_extra")
                if isinstance(suffix, ast.Name) and suffix.id in module_constants:
                    suffix = module_constants[suffix.id]
                source = ast.unparse(suffix) if suffix is not None else None
                if source != USER_AGENT_FROM_ENVIRONMENT:
                    problems.append(f"{where}: Config(...) with user_agent_extra={source}")
    assert not problems, problems
    assert clients >= 12, clients  # the walk found the clients; an empty walk would pass vacuously


# The libraries of the Lambda layers and the Pydantic models that the two generation functions import. The
# unit test environment has none of them, so empty stand-ins take their place, as test_agent_handler.py does
# for the SDKs of the agent: these tests look only at the clients of the functions.
LAYER_STAND_INS = {
    "PyPDF2": {},
    "langchain_core": {},
    "langchain_core.prompts": {"PromptTemplate": object},
    "pydantic_utils": {"convert_pydantic_to_bedrock_converse_function": lambda model: {}},
    "CourseOutlinePydantic": {"CourseOutline": object},
    "CourseContentPydantic": {"CourseContent": object},
}
GENERATION_FUNCTIONS = ("course_content_llm", "course_outline_llm")

# What the generation functions ask of Amazon S3 and of Amazon Bedrock
SYLLABUS_OBJECT = {"Bucket": "amzn-s3-demo-bucket", "Key": "syllabus.pdf"}
CONVERSE_REQUEST = {"modelId": "us.anthropic.claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": [{"text": "question"}]}]}

# The clients that the Lambda modules hold or return: file, how to get the client, an operation to call.
# The generation functions create one more client in their handler; it has its own tests below.
LOADABLE_LAMBDA_CLIENTS = {
    "connect": ("lambda/connect/index.py", lambda module: module.dynamodb, "list_tables", {}),
    "disconnect": ("lambda/disconnect/index.py", lambda module: module.dynamodb, "list_tables", {}),
    "default": (
        "lambda/default/index.py",
        lambda module: module.get_api_client(
            {"requestContext": {"domainName": "example.execute-api.us-east-1.amazonaws.com", "stage": "dev"}}),
        "get_connection", {"ConnectionId": "connection"},
    ),
    "course_outline_ws": ("lambda/course_outline_ws/helper.py", lambda module: module.sqs_client, "list_queues", {}),
    "course_content_ws": ("lambda/course_content_ws/helper.py", lambda module: module.sqs_client, "list_queues", {}),
    "kb_sync": ("lambda/kb_sync/index.py", lambda module: module.bedrock_agent_client, "list_knowledge_bases", {}),
    "qna_bot_classic": (
        "lambda/qna_bot_classic/helper.py", lambda module: module.bedrock_agent_runtime_client,
        "retrieve", {"knowledgeBaseId": "KB00000001", "retrievalQuery": {"text": "question"}},
    ),
    "qna_bot_agentcore_proxy": (
        "lambda/qna_bot_agentcore_proxy/index.py", lambda module: module.agentcore_client,
        "invoke_agent_runtime", {"agentRuntimeArn": "example-runtime-arn", "payload": b"{}"},
    ),
    "course_outline_llm_s3": (
        "lambda/course_outline_llm/helper.py", lambda module: module.s3_client, "get_object", SYLLABUS_OBJECT),
    "course_outline_llm_bedrock": (
        "lambda/course_outline_llm/helper.py", lambda module: module.bedrock_runtime_client,
        "converse", CONVERSE_REQUEST),
    "course_content_llm_s3": (
        "lambda/course_content_llm/helper.py", lambda module: module.s3_client, "get_object", SYLLABUS_OBJECT),
    "course_content_llm_bedrock": (
        "lambda/course_content_llm/helper.py", lambda module: module.bedrock_runtime_client,
        "converse", CONVERSE_REQUEST),
}


def load_lambda_module(patch, relative_path, user_agent):
    """Load a Lambda source file as the Lambda runtime does, with placeholders instead of real AWS settings
    and with stand-ins for the libraries of the Lambda layers."""
    if user_agent is None:
        patch.delenv("USER_AGENT_STRING", raising=False)
    else:
        patch.setenv("USER_AGENT_STRING", user_agent)
    patch.setenv("AGENT_RUNTIME_ARN", "example-runtime-arn")
    patch.setenv("CONNECTIONS_TABLE", "connections")
    patch.setenv("INPUT_BUCKET_NAME", "amzn-s3-demo-bucket")
    patch.setenv("OUTPUT_BUCKET", "amzn-s3-demo-bucket")
    patch.setenv("WEBSOCKET_ENDPOINT_URL", "https://example.execute-api.us-east-1.amazonaws.com/dev")
    for name, attributes in LAYER_STAND_INS.items():
        stand_in = types.ModuleType(name)
        vars(stand_in).update(attributes)
        patch.setitem(sys.modules, name, stand_in)
    patch.setenv("AWS_REGION", "us-east-1")
    patch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        patch.setenv(name, "testing")
    # Keep the AWS configuration of this machine out of the clients
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        patch.delenv(name, raising=False)
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        patch.setenv(name, os.devnull)
    patch.setattr(boto3, "DEFAULT_SESSION", None)
    # Keep the import from writing __pycache__ into the folder that CDK packages as the function code
    patch.setattr(sys, "dont_write_bytecode", True)
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(f"wiring_{path.parent.name}_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    # Some handlers set the level of the root logger when they are loaded; put it back for the other tests
    root_logger = logging.getLogger()
    level = root_logger.level
    try:
        spec.loader.exec_module(module)
    finally:
        root_logger.setLevel(level)
    return module


@pytest.mark.parametrize("name", sorted(LOADABLE_LAMBDA_CLIENTS))
def test_lambda_client_sends_the_token_once(name, sent_user_agent):
    relative_path, client_of, operation, parameters = LOADABLE_LAMBDA_CLIENTS[name]
    token = solution.user_agent_string()
    with pytest.MonkeyPatch.context() as patch:
        module = load_lambda_module(patch, relative_path, token)
        user_agent = sent_user_agent(client_of(module), operation, **parameters)
    # The attribution matches whole tokens that are separated by spaces
    assert user_agent.split().count(token) == 1, user_agent


@pytest.mark.parametrize("name", sorted(LOADABLE_LAMBDA_CLIENTS))
def test_lambda_client_works_without_the_variable(name, sent_user_agent):
    """Without the solution block the stacks do not set the variable. The functions must still start, and
    their requests must then carry no solution token at all."""
    relative_path, client_of, operation, parameters = LOADABLE_LAMBDA_CLIENTS[name]
    prefix = solution.user_agent_string().split("/")[0] + "/"
    with pytest.MonkeyPatch.context() as patch:
        module = load_lambda_module(patch, relative_path, None)
        user_agent = sent_user_agent(client_of(module), operation, **parameters)
    assert not [field for field in user_agent.split() if field.startswith(prefix)], user_agent
    assert user_agent == user_agent.strip(), repr(user_agent)


def test_the_proxy_client_keeps_its_retry_settings():
    """The suffix is added to the existing client configuration of the proxy, not put in its place."""
    with pytest.MonkeyPatch.context() as patch:
        module = load_lambda_module(patch, "lambda/qna_bot_agentcore_proxy/index.py", solution.user_agent_string())
        retries = module.agentcore_client.meta.config.retries
    assert retries["mode"] == "standard" and retries["total_max_attempts"] == 3, retries


@pytest.mark.parametrize("function", GENERATION_FUNCTIONS)
def test_the_bedrock_client_of_a_generation_function_keeps_its_timeouts(function):
    """The suffix is added to the client configuration that raises the timeouts for long model responses,
    not put in its place."""
    with pytest.MonkeyPatch.context() as patch:
        module = load_lambda_module(patch, f"lambda/{function}/helper.py", solution.user_agent_string())
        configuration = module.bedrock_runtime_client.meta.config
    assert (configuration.connect_timeout, configuration.read_timeout) == (300, 300)


def user_agents_of_a_rejected_request(function, user_agent):
    """Run the handler of a generation function with a request that it rejects, and return the ``User-Agent``
    header of every request that it makes for it.

    The handler creates its API Gateway client itself, on every invocation, and reports the rejection to the
    caller through it. Every client of the function gets the interception when it is created, so the requests
    are recorded and stopped before they are sent and nothing reaches AWS; the handler logs the error of
    that call and returns.
    """
    sent = []

    def intercept(request, **_kwargs):
        header = request.headers["User-Agent"]
        sent.append(header.decode() if isinstance(header, bytes) else header)
        raise RuntimeError("request intercepted before it was sent")

    create_client = botocore.session.Session.create_client

    def create_intercepted_client(session, *args, **kwargs):
        client = create_client(session, *args, **kwargs)
        client.meta.events.register("before-send.*.*", intercept)
        return client

    record = {"requestContext": {"connectionId": "connection"}, "body": "not a JSON document"}
    with pytest.MonkeyPatch.context() as patch:
        # boto3 creates every client, of any session, with this method of botocore
        patch.setattr(botocore.session.Session, "create_client", create_intercepted_client)
        helper = load_lambda_module(patch, f"lambda/{function}/helper.py", user_agent)
        # The handler imports its helper module by name; the patch removes it again after the test
        patch.setitem(sys.modules, "helper", helper)
        handler = load_lambda_module(patch, f"lambda/{function}/index.py", user_agent)
        response = handler.lambda_handler({"Records": [{"body": json.dumps(record)}]}, None)
    assert response["statusCode"] == 400, response
    return sent


@pytest.mark.parametrize("function", GENERATION_FUNCTIONS)
def test_the_handler_client_of_a_generation_function_sends_the_token_once(function):
    token = solution.user_agent_string()
    user_agents = user_agents_of_a_rejected_request(function, token)
    assert len(user_agents) == 1, user_agents
    assert user_agents[0].split().count(token) == 1, user_agents


@pytest.mark.parametrize("function", GENERATION_FUNCTIONS)
def test_the_handler_client_of_a_generation_function_works_without_the_variable(function):
    prefix = solution.user_agent_string().split("/")[0] + "/"
    user_agents = user_agents_of_a_rejected_request(function, None)
    assert len(user_agents) == 1, user_agents
    assert not [field for field in user_agents[0].split() if field.startswith(prefix)], user_agents


def test_strands_keeps_the_token_next_to_its_own(sent_user_agent):
    """The agent gives the token to Strands in a client configuration. Strands must append its own
    user-agent token to it, not replace it, and must leave the read timeout of that configuration alone.

    Runs where the packages of the agent are installed (lambda/qna_bot_agentcore/requirements.txt); the
    unit test environment does not need them.
    """
    strands_bedrock = pytest.importorskip("strands.models.bedrock")
    from botocore.config import Config

    token = solution.user_agent_string()
    with pytest.MonkeyPatch.context() as patch:
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            patch.setenv(name, "testing")
        for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            patch.delenv(name, raising=False)
        for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
            patch.setenv(name, os.devnull)
        model = strands_bedrock.BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-6", region_name="us-east-1",
            boto_client_config=Config(user_agent_extra=token, read_timeout=120),
        )
        user_agent = sent_user_agent(model.client, "converse", modelId="us.anthropic.claude-sonnet-4-6",
                                     messages=[{"role": "user", "content": [{"text": "question"}]}])
    fields = user_agent.split()
    assert fields.count(token) == 1, user_agent
    assert "strands-agents" in fields, user_agent
    assert model.client.meta.config.read_timeout == 120


# --- API usage attribution: code that runs from a checkout -----------------------------------------

def test_code_that_runs_from_a_checkout_creates_its_clients_with_the_helpers():
    """The helper scripts and the demo can import solution.py, so they use get_client() and get_resource().
    A bare boto3.client() or Session().client() works and is never attributed, so there must be none
    outside lambda/ (covered above) and the reader itself."""
    reader = Path(solution.__file__).resolve()
    bare_clients = []
    checked = 0
    for relative_path in shipped_files():
        path = REPO_ROOT / relative_path
        if path.suffix != ".py" or relative_path.parts[0] in ("lambda", "tests") or path == reader:
            continue
        checked += 1
        calls, _module_constants = _calls(path)
        bare_clients += [f"{relative_path}:{call.lineno}: {ast.unparse(call.func)}()" for call in calls
                         if isinstance(call.func, ast.Attribute) and call.func.attr in CLIENT_FACTORIES]
    assert not bare_clients, bare_clients
    assert checked >= 6, checked  # app.py, the three stacks, the helper scripts and the demo were read


# Helper scripts: file, and a call that makes the script create a client and send a request
HELPER_SCRIPT_REQUESTS = {
    "create_cognito_user": ("scripts/create_cognito_user.py",
                            lambda module: module.get_stack_outputs("us-east-1", "CourseStack")),
    "invoke_agentcore_runtime": ("scripts/invoke_agentcore_runtime.py",
                                 lambda module: module.get_agent_runtime_arn("us-east-1")),
}


@pytest.mark.parametrize("name", sorted(HELPER_SCRIPT_REQUESTS))
def test_helper_script_sends_the_token_once(name):
    relative_path, make_request = HELPER_SCRIPT_REQUESTS[name]
    token = solution.user_agent_string()
    sent = []

    def intercept(request, **_kwargs):
        header = request.headers["User-Agent"]
        sent.append(header.decode() if isinstance(header, bytes) else header)
        raise RuntimeError("request intercepted before it was sent")

    create_client = botocore.session.Session.create_client

    def create_intercepted_client(session, *args, **kwargs):
        client = create_client(session, *args, **kwargs)
        client.meta.events.register("before-send.*.*", intercept)
        return client

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(botocore.session.Session, "create_client", create_intercepted_client)
        patch.setattr(boto3, "DEFAULT_SESSION", None)
        patch.setattr(sys, "dont_write_bytecode", True)
        # The script puts the repository root on the module path; take it off again after the test
        patch.setattr(sys, "path", list(sys.path))
        path = REPO_ROOT / relative_path
        spec = importlib.util.spec_from_file_location(f"wiring_script_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with pytest.raises(RuntimeError, match="intercepted"):
            make_request(module)
    assert len(sent) == 1, sent
    assert sent[0].split().count(token) == 1, sent


# --- Opting out ------------------------------------------------------------------------------------

def test_without_a_token_no_function_and_no_runtime_gets_the_variable(monkeypatch):
    """Without the solution block there is no token (test_solution.py covers the reader). The stacks must
    then leave the variable out instead of setting an empty value."""
    from aws_cdk.assertions import Template

    from educational_course_content_generator_with_qna_bot_using_bedrock import course_stack, qna_stack
    from educational_course_content_generator_with_qna_bot_using_bedrock.cloudfront_waf_stack import (
        CloudFrontWAFStack,
    )

    monkeypatch.setattr(course_stack, "user_agent_string", lambda: "")
    monkeypatch.setattr(qna_stack, "user_agent_string", lambda: "")
    monkeypatch.chdir(REPO_ROOT)
    with open(REPO_ROOT / "cdk.json", encoding="utf-8") as cdk_json:
        app = cdk.App(context={**json.load(cdk_json)["context"], "aws:cdk:bundling-stacks": []})
    # The application supplies its own descriptions when it opts out
    CloudFrontWAFStack(app, "WafStack", description="own description")
    course = course_stack.CourseStack(app, "CourseStack", description="own description")
    qna = qna_stack.QnAStack(app, "QnAStack", vpc=course.vpc, description="own description")

    for stack in (course, qna):
        template = Template.from_stack(stack).to_json()
        assert template["Description"] == "own description"
        resources = template["Resources"].values()
        functions = [resource for resource in resources if resource["Type"] == "AWS::Lambda::Function"]
        assert functions
        for resource in functions:
            variables = resource["Properties"].get("Environment", {}).get("Variables", {})
            assert "USER_AGENT_STRING" not in variables, variables
        for resource in resources:
            if resource["Type"] == "AWS::BedrockAgentCore::Runtime":
                assert "USER_AGENT_STRING" not in resource["Properties"]["EnvironmentVariables"]
