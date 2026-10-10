# Thordata SERP for Dify

## Overview

Thordata SERP is a Dify plugin for live, structured search through the Thordata
SERP API. The package exposes 34 engine-specific tools generated from the
Thordata Schema. This is 34 generated tools in the installed provider. Selecting an
engine means selecting its generated Dify Tool (for example, `google_web`,
`google_news`, or `bing_news`), so the editor shows the parameters that belong to
that engine instead of one generic parameter bag.

## Features

- Ships one generated Dify Tool for each validated engine in the Thordata Schema.
- Validates engine-specific parameters before a search request is sent.
- Uses the runtime Schema repository (remote, cache, or snapshot) to validate
  and execute requests.
- Supports compact and complete structured responses.
- Redacts the configured API key from expected errors.

## Requirements

- Dify with Python 3.12 plugin runtime support.
- A Thordata SERP API key with access to the requested engines.
- Outbound HTTPS access to the two domains listed under Network Access.

The package validates against the current Dify plugin SDK and the target
deployment. `manifest.yaml` does not declare a tested minimum Dify version.

## Installation

Download or build the local `.difypkg` file, open the Dify plugin management page,
choose the option to install a local plugin file, and select the current package
at `dist/thordata_serp-0.1.0.difypkg` (or the `.difypkg` produced by your build).
Review the requested network domains before enabling the plugin.

## Authentication

Create an API key in the Thordata dashboard. In Dify, open the Thordata SERP provider,
enter the key in the secret credential field, and save it. Do not put the key in tool
arguments, prompts, workflow variables intended for display, or source control.

## Credential Validation

Saving or validating the provider credential makes a request with engine key `google`,
`num=1`, and the query `Dify credential validation`. Therefore, credential validation may consume one successful SERP request. Dify reports a safe validation error if the request
cannot be completed.

## Tools

### Generated engine tools

The provider installs 34 generated tools, one per Schema engine. Each tool fixes its
engine key and presents that engine's fields in the Dify editor. The generated YAML
and Python wrapper are checked in under `tools/generated/`; they are the installed
tool contract and are intentionally static until the package is rebuilt and
reinstalled.

The generator maps Schema controls to Dify controls in the same way as the n8n
integration (input/string, number, select/dynamic-select, boolean/switch, dynamic-select
multi-select, and object/JSON):

- `input` becomes a string input; `number` becomes a number input.
- `select` becomes `dynamic-select` with the declared options; `switch` becomes a
  boolean/switch control.
- `multi_select` becomes a dynamic-select control with multiple values enabled.
- `json` becomes an object/JSON control.
- Google Web's `cr` and `lr` fields use the generated country and language options.
- Location fields use generated location options. When an engine exposes both visible
  `location` and `uule` fields, automatic UULE generation is applied from the selected
  location and the location description explains that behavior; `uule` is not exposed as
  a user-editable field.

Every generated tool also has an explicit `extra_parameters_json` advanced JSON
fallback for fields that are not convenient to enter in the form. The fallback is
validated as an object, and the extra `engine` and `isjson` fields remain protected;
the protected pair is engine and isjson, and they cannot override the fixed engine or
the structured response mode.

For engines that support location targeting, advanced JSON can provide an explicit
`uule` value to override the runtime-derived value.

Generated tools keep response format `1` (structured JSON) and response mode `compact`
internally, so these controls are not shown in the node settings. The advanced
`extra_parameters_json` field may override response settings when supported.

For compatibility with older integrations, a generic JSON request may still be
available in a deployment that installed an earlier package. Treat that path as an
advanced fallback only. The primary Dify workflow is always to choose the generated
tool for the desired engine.

### Dynamic-select compatibility

Generated `dynamic-select` controls include validated snapshot options in the checked-in
YAML so the Dify editor has useful static choices immediately after installation. On
Dify versions that support runtime dynamic option fetching, the generated wrapper can
refresh options from the current Schema; the runtime then falls back to the snapshot
when the Schema endpoint is unavailable. Do not assume every Dify version supports
runtime dynamic option fetching. The static options and runtime validation remain the
compatibility path.

## Schema Source and Generated Artifacts

The API is the source of truth for the Thordata Schema. The generator reads either the
remote Schema or a validated local snapshot, then emits the generated YAML and Python wrappers.
Generated YAML is checked in because Dify installed Tool parameters are static; a
running plugin cannot change the editor contract in place. At runtime, the plugin still
refreshes the Schema and its cache/snapshot to validate parameters and execute requests.

When the API adds a field or engine, rerun the generator, perform the package rebuild,
and reinstall it before that change appears in the Dify editor. A runtime Schema refresh
alone does not add a new installed Tool or editor field.

## Agent Usage

Give an agent the generated tools for the engines it may use. Instruct it to select the
engine-specific Dify Tool, provide the required query, and use the generated controls
or `extra_parameters_json` for advanced fields. The tool description and fixed engine
key prevent the agent from guessing an engine or mixing fields from another engine.

## Workflow Usage

In a Dify workflow, add the generated Tool for the desired engine and map the query and
engine-specific fields directly. Use the generated select, dynamic-select, switch,
multi-select, number, and JSON controls instead of assembling a generic
`engine + parameters_json` request. Use `extra_parameters_json` only as the explicit
advanced fallback. Route the result to later nodes and handle the tool error branch
rather than exposing raw errors to end users.

## Dynamic Schema Behavior

The plugin requests the current Schema on first use and keeps a five-minute in-memory
cache. The `schema_source` value is `remote` after a successful fetch, `cache` when a
previous validated Schema is reused, or `snapshot` when the bundled fallback is used.
Runtime validation applies to generated form values and the advanced JSON fallback.

## Network Access

The plugin makes outbound HTTPS requests only to these declared endpoints:

- `https://api.thordata.com/serp/playground/schema?lang=en` on `api.thordata.com`
  retrieves the engine Schema and sends no API key.
- `https://scraperapi.thordata.com/request` on `scraperapi.thordata.com` performs
  authenticated SERP searches.

Redirects are not followed. Endpoint overrides are not exposed by the plugin.

## Error Handling

Schema, parameter, request, response, and upstream API failures are normalized before
they cross the tool boundary. Expected errors are bounded and redact the configured
API key. Failed tools do not print or return the complete search result as diagnostic
output. Dify workflows should treat a tool failure as an explicit error branch.

## Maintenance

From the `dify` source directory, generate all engine tools from the bundled validated
snapshot and atomically update the checked-in YAML/Python files and provider list:

```bash
python scripts/generate_engine_tools.py --snapshot --write
```

Check that the checked-in generated files match the snapshot without changing files:

```bash
python scripts/generate_engine_tools.py --check
```

For a controlled remote refresh, use the secure process-local environment or a
non-echoing prompt supplied by your operating system or CI secret store. Never commit
credentials. Never put tokens in argv or shell history. The remote Schema endpoint
is validated before generation; no credential is written to generated files.

The older snapshot updater remains available for maintaining the runtime fallback:

```bash
python scripts/update_schema_snapshot.py
```

Before running the opt-in production smoke request, set `THORDATA_SERP_API_KEY` through
an operating-system or CI secret store, a protected process environment, or a
non-echoing shell prompt. Do not put the key in command-line arguments or shell history.
Tests do not require a real key or network access.

The smoke helper is:

For POSIX shells:

```bash
python scripts/smoke_serp.py
```

For PowerShell:

```powershell
python scripts/smoke_serp.py
```

For Windows cmd.exe:

```bat
python scripts\smoke_serp.py
```

## Support

For integration support, contact `cianna@thordata.com`.

## Source

The public source repository is
<https://github.com/Thordata/dify-plugin-thordata-serp>.

## Privacy

See [PRIVACY.md](PRIVACY.md) for the data flow, credential handling, retention, and
user-control disclosures.

## License

This plugin is released under the MIT License. See [LICENSE](LICENSE).
