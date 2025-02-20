## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
from typing_extensions import TypedDict
from typing import Any, Dict, List, Optional, Type, Sequence, Set
from pydantic import BaseModel
from copy import deepcopy

class FunctionDescription(TypedDict):
    """Representation of a callable function to send to an LLM."""

    name: str
    """The name of the function."""
    description: str
    """A description of the function."""
    parameters: dict
    """The parameters of the function."""


def _retrieve_ref(path: str, schema: dict) -> dict:
    components = path.split("/")
    if components[0] != "#":
        raise ValueError(
            "ref paths are expected to be URI fragments, meaning they should start "
            "with #."
        )
    out = schema
    for component in components[1:]:
        if component.isdigit():
            out = out[int(component)]
        else:
            out = out[component]
    return deepcopy(out)


def _dereference_refs_helper(
    obj: Any,
    full_schema: Dict[str, Any],
    skip_keys: Sequence[str],
    processed_refs: Optional[Set[str]] = None,
) -> Any:
    if processed_refs is None:
        processed_refs = set()

    if isinstance(obj, dict):
        obj_out = {}
        for k, v in obj.items():
            if k in skip_keys:
                obj_out[k] = v
            elif k == "$ref":
                if v in processed_refs:
                    continue
                processed_refs.add(v)
                ref = _retrieve_ref(v, full_schema)
                full_ref = _dereference_refs_helper(
                    ref, full_schema, skip_keys, processed_refs
                )
                processed_refs.remove(v)
                return full_ref
            elif isinstance(v, (list, dict)):
                obj_out[k] = _dereference_refs_helper(
                    v, full_schema, skip_keys, processed_refs
                )
            else:
                obj_out[k] = v
        return obj_out
    elif isinstance(obj, list):
        return [
            _dereference_refs_helper(el, full_schema, skip_keys, processed_refs)
            for el in obj
        ]
    else:
        return obj


def _infer_skip_keys(
    obj: Any, full_schema: dict, processed_refs: Optional[Set[str]] = None
) -> List[str]:
    if processed_refs is None:
        processed_refs = set()

    keys = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "$ref":
                if v in processed_refs:
                    continue
                processed_refs.add(v)
                ref = _retrieve_ref(v, full_schema)
                keys.append(v.split("/")[1])
                keys += _infer_skip_keys(ref, full_schema, processed_refs)
            elif isinstance(v, (list, dict)):
                keys += _infer_skip_keys(v, full_schema, processed_refs)
    elif isinstance(obj, list):
        for el in obj:
            keys += _infer_skip_keys(el, full_schema, processed_refs)
    return keys


def dereference_refs(
    schema_obj: dict,
    *,
    full_schema: Optional[dict] = None,
    skip_keys: Optional[Sequence[str]] = None,
) -> dict:
    """Try to substitute $refs in JSON Schema."""

    full_schema = full_schema or schema_obj
    skip_keys = (
        skip_keys
        if skip_keys is not None
        else _infer_skip_keys(schema_obj, full_schema)
    )
    return _dereference_refs_helper(schema_obj, full_schema, skip_keys)


def _rm_titles(kv: dict, prev_key: str = "") -> dict:
    new_kv = {}
    for k, v in kv.items():
        if k == "title":
            if isinstance(v, dict) and prev_key == "properties" and "title" in v.keys():
                new_kv[k] = _rm_titles(v, k)
            else:
                continue
        elif isinstance(v, dict):
            new_kv[k] = _rm_titles(v, k)
        else:
            new_kv[k] = v
    return new_kv

    
def convert_pydantic_to_bedrock_converse_function(
    model: Type[BaseModel],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    rm_titles: bool = True,
) -> FunctionDescription:
    """Converts a Pydantic model to a function description for the Bedrock Anthropic API."""
    schema = dereference_refs(model.model_json_schema())
    schema.pop("definitions", None)
    title = schema.pop("title", "")
    default_description = schema.pop("description", "")
    
    bedrock_json = {
                "toolSpec": {
                    "name": name or title,
                    "description": description or default_description,
                    "inputSchema": {
                        "json": _rm_titles(schema) if rm_titles else schema
                    }
                }
            }
    return bedrock_json
    
