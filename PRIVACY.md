# Privacy Policy for Thordata SERP for Dify

## Data Processed

The plugin processes the configured Thordata API key, the user's query, the selected
engine, search parameters, Schema data, response metadata, and search results needed
to execute each requested generated tool operation. Generated tools do not add data collection.
They add no data collection beyond the data required by the SERP request.

## Purpose

This data is processed only to discover supported search engines, validate a provider
credential, construct a requested SERP search, return its result to Dify, and provide
bounded error information when an operation fails.

## Transmission

Schema requests retrieve the public engine definition. The remote schema fetch sends no API key. The API key,
query, selected engine, search parameters, and search results are sent to Thordata via
HTTPS when a search or credential validation request is made. Search responses are
returned to the Dify runtime.

## Credential Handling

Dify manages credential storage. The plugin receives the API key from Dify at runtime,
processes it in memory, uses it to authenticate requested searches, and redacts it from
expected error messages. Users should not place credentials in prompts, queries, or
search parameters.

## Data Retention

The plugin processes credentials, queries, parameters, and results in memory. There is no
plugin persistence. It does not persist data, and there is no independent logging. The
plugin does not independently log the API key, query, selected engine, search parameters,
or results. A validated public engine Schema may be held in an
in-memory cache for five minutes; the package also includes a public Schema snapshot.

## Third-Party Service

Search requests are handled by Thordata according to its published terms and privacy
practices. Review the [Thordata website](https://www.thordata.com),
[Thordata privacy policy](https://www.thordata.com/privacy-policy), and
[Thordata documentation](https://doc.thordata.com) before using the service.

## User Control

Users choose whether to install the plugin, configure a credential, validate it, and
run each generated tool. Removing the provider credential or uninstalling the plugin
prevents new authenticated requests from this integration. Dify administrators control
Dify's own workflow data, logs, and retention settings.

## Changes

This policy may be updated when the plugin's data flow or service integration changes.
Material updates will be published with the plugin source and release materials.

## Contact

Questions about this integration can be sent to `cianna@thordata.com`. Source and issue
tracking are available at <https://github.com/Thordata/dify-plugin-thordata-serp>.
