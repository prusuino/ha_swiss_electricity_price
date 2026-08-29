# Security Policy

## Supported Versions

Only the latest release is supported. Please update to the latest version before reporting an issue.

## Reporting a Vulnerability

This integration only reads publicly published data from official Swiss data sources (ElCom / individual grid operators) — it does not handle credentials, personal data, or write access to any external system. Its complete external traffic is:

- `POST https://cached.lindas.admin.ch/query` (LINDAS, the Swiss federal Linked Data Service): SPARQL queries for ElCom's operator directory and, once per update, for the configured operator's currently registered link. The only thing sent is the query text, which contains the public ElCom operator identifier.
- `GET` of the tariff-file link the configured operator registered with ElCom (or of the URL you entered yourself during setup), plus the JSON file that link resolves to. Registered links are fetched exactly as the operator reported them to ElCom — which means plain `http://` when the operator registered its file that way; the integration neither upgrades nor downgrades the scheme.
- During setup only: one `GET` against the registered link of each operator shown in the picker whose registered link points at a `.json` file (at most 60 per search), to label it as a working direct link or not; operators whose registered link is a plain web page are labeled auto-detect without any request.

If you still believe you have found a security issue (e.g. in how data is parsed or how entities are exposed), please report it privately via [GitHub Security Advisories](../../security/advisories/new) rather than opening a public issue.

For anything that is not security-sensitive (bugs, feature requests), please use the regular [Issues](../../issues) tab instead.
