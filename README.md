## 🚀 Thordata SERP Plugin for Dify

**Connect Dify to real-time Google, Bing, and more for web, news, images, maps, shopping, scholar, and flights with the [**Thordata SERP API**](https://www.thordata.com/?ls=github01&lk=github01). Build AI agents and workflows powered by fresh, structured, and reliable search data.**

The Thordata SERP Dify plugin adds the Thordata SERP API as Dify tools, helping you use search result data in Dify workflows and agents. The installed provider ships 34 engine-specific tools generated from the Thordata Schema.

### Get an API Token

Enter your Thordata SERP API Token in the plugin authorization settings.

**Create a Thordata account and get your API Token:**

1. [**Create a free Thordata account**](https://www.thordata.com/?ls=github01&lk=github01)
2. Go to the API Token page in the Thordata dashboard.
3. Create or copy an available API Token.
4. Paste the Token into the Dify plugin configuration.

🎉 New users receive free trial credits after signing up, allowing you to start building with Dify immediately.

Do not put the Token in tool arguments, prompts, workflow variables intended for display, or source control.

### **Why Thordata?**

**Thordata provides real-time, structured search data designed for AI applications. With a single plugin, your Dify workflows can access multiple search engines and return LLM-ready JSON responses.**

**Key benefits:**

- **Real-time web, news, images, maps, shopping, scholar, and flights search across Google, Bing, and more**
- **Structured JSON responses optimized for LLMs**
- **34 generated engine tools, one per validated Schema engine**
- **Native integration with Dify workflows and agents**
- **Free trial credits for new users**

### Features

- Ships one generated Dify Tool for each validated engine in the Thordata Schema.
- Validates every request against the engine's current parameter schema, with a bundled
  fallback when the schema endpoint is unreachable, before it is sent.
- Supports compact and complete structured responses.
- Redacts the configured API Token from expected errors.

### Requirements

- Dify with Python 3.12 plugin runtime support.
- A Thordata SERP API Token with access to the requested engines.
- Outbound HTTPS access to the two domains listed under Network Access.

Verified against self-hosted Dify 1.17.1.

### Installation

Install the plugin from the Dify Marketplace. For a local build, open the Dify plugin
management page, choose the option to install a local plugin file, and select the
`.difypkg` you produced from this repository. Review the requested network domains
before enabling the plugin.

### Credential Validation

Saving or validating the provider credential makes one real search request (engine
`google`, query `Dify credential validation`), so validation may consume one successful
SERP request. Dify reports a safe validation error if the request cannot be completed.

### Tools

#### Generated engine tools

The provider installs 34 generated tools, one per Schema engine. Each tool fixes its
engine key and presents that engine's fields in the Dify editor, so the form only ever
offers parameters the selected engine accepts.

Parameters that take one of a known set of values — country, language, domain, safe
search — are rendered as dropdowns, and the country and language filters (`cr`, `lr`)
accept several values. Location fields list the supported places: picking a place is
enough, because on engines that also accept an encoded location the plugin derives it
from the selected place and shows the derived value in the option label. There is no
separate encoded-location field to fill in.

Every generated tool also has an `extra_parameters_json` field for settings that are not
convenient to enter in the form. It only sets parameters the engine declares and keeps
visible — in practice two of them: an explicit encoded location (`uule`), and `json` to
override the response format. The fixed engine and the structured response mode cannot be
changed: `engine` and `isjson` are ignored. Names the engine does not declare — including
schema-hidden ones such as `ai_overview` — are dropped silently, without an error.

Generated tools always ask for structured JSON in compact mode, so the response-format
and response-mode controls are not shown in the node settings.

This package does not provide a generic `engine + parameters_json` request entry point:
every search goes through the generated tool for the engine you want. New engines and
fields arrive with a plugin update — refreshing the Schema alone does not add a new
installed tool.

#### Dropdown options

Dropdowns ship with validated options, so the editor is usable the moment the plugin is
installed. On Dify versions that support it, the plugin also refreshes those options from
the current Schema, and falls back to the bundled values when the Schema endpoint is
unavailable.

### Agent Usage

Give an agent the generated tools for the engines it may use. Instruct it to select the
engine-specific Dify Tool, provide the required query, and use the generated controls
or `extra_parameters_json` for advanced fields. The tool description and fixed engine
key prevent the agent from guessing an engine or mixing fields from another engine.

### Workflow Usage

In a Dify workflow, add the generated Tool for the desired engine and map the query and
engine-specific fields directly. Use the generated select, dynamic-select, switch,
multi-select, number, and JSON controls instead of assembling a generic
`engine + parameters_json` request. Use `extra_parameters_json` only as the explicit
advanced fallback. Route the result to later nodes and handle the tool error branch
rather than exposing raw errors to end users.

### Network Access

The plugin makes outbound HTTPS requests only to these declared endpoints:

- `https://api.thordata.com/serp/playground/schema?lang=en` on `api.thordata.com`
  retrieves the engine Schema and sends no credential.
- `https://scraperapi.thordata.com/request` on `scraperapi.thordata.com` performs
  authenticated SERP searches.

Redirects are not followed and the endpoints cannot be overridden.

### Error Handling

Failures — schema, parameter, request, response or upstream API — are normalized before
they cross the tool boundary and never contain the configured API Token. In a workflow,
route a failed tool call down an explicit error branch instead of exposing the raw error
to end users.

### Support

For issues with the Dify plugin package, report an issue in the [**Thordata Dify plugin repository**](https://github.com/Thordata/dify-plugin-thordata-serp).

For Thordata SERP API account, quota, or API Token issues, contact Thordata support through the [**Thordata website**](https://www.thordata.com/?ls=github01&lk=github01).

This plugin is released under the MIT License; see [LICENSE](LICENSE). Data flow, credential handling, retention, and user-control disclosures are in [PRIVACY.md](PRIVACY.md).

**For detailed integration tutorials and API documentation, visit the **[**Thordata Documentation**](https://doc.thordata.com)**.**

### Learn More

Ready to build AI agents with real-time search?

- Explore the [**Thordata SERP API**](https://www.thordata.com/?ls=github01&lk=github01)
- Read the [**SERP API Documentation**](https://doc.thordata.com/doc/scraping/serp-api)
- [**Create a free Thordata account and start with trial credits**](https://www.thordata.com/?ls=github01&lk=github01)

Thordata brings real-time search to Dify, helping developers build AI agents and workflows with fresh, structured, and reliable search data.
