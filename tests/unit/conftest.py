# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixtures shared by the unit tests."""
import os

import pytest


@pytest.fixture(autouse=True)
def aws_settings_of_the_machine_stay_out(monkeypatch):
    """No test may depend on the AWS settings of the machine it runs on, or pass or fail because of them:
    a profile that the environment names, a configuration file, real credentials, or a USER_AGENT_STRING
    variable, which would replace the solution token of project_config.json. Placeholder credentials are
    enough to sign the requests that the tests intercept before they are sent."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "USER_AGENT_STRING"):
        monkeypatch.delenv(name, raising=False)
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.setenv(name, os.devnull)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")


class _Intercepted(Exception):
    """Carries the request headers out of botocore's send path."""

    def __init__(self, headers):
        super().__init__("request intercepted before it was sent")
        self.headers = headers


@pytest.fixture
def sent_user_agent():
    """Returns a function that makes ``client`` build and sign a real request, intercepts the request
    before it is sent and returns its ``User-Agent`` header. Nothing reaches AWS.

    The header is what the usage attribution reads. A test on the ``Config`` object of a client would pass
    while the header is wrong, because botocore is free to rewrite what it is given.
    """
    def capture(client, operation, **parameters):
        def intercept(request, **_kwargs):
            raise _Intercepted(request.headers)

        client.meta.events.register("before-send.*.*", intercept)
        try:
            getattr(client, operation)(**parameters)
        except _Intercepted as intercepted:
            user_agent = intercepted.headers["User-Agent"]
            return user_agent.decode() if isinstance(user_agent, bytes) else user_agent
        finally:
            client.meta.events.unregister("before-send.*.*", intercept)
        raise AssertionError("the request was not intercepted, so no header was captured")

    return capture
