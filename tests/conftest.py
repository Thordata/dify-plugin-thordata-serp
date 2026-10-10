from copy import deepcopy

import pytest


@pytest.fixture
def schema_payload():
    return deepcopy(
        {
            "code": 0,
            "msg": "ok",
            "data": {
                "schema_version": "1",
                "audience": "is_serp_old=0",
                "default_engine": "google",
                "categories": [
                    {
                        "key": "google",
                        "name": "Google",
                        "engines": [
                            {
                                "key": "google",
                                "name": "Search",
                                "query_field": "q",
                                "groups": [
                                    {
                                        "key": "parameters",
                                        "name": "Parameters",
                                        "fields": [
                                            {
                                                "key": "q",
                                                "label": "Search Query",
                                                "type": "string",
                                                "control": "input",
                                                "required": True,
                                                "visible": True,
                                            },
                                            {
                                                "key": "num",
                                                "label": "Result Count",
                                                "type": "number",
                                                "control": "number",
                                                "required": False,
                                                "visible": True,
                                                "default": 10,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    )
