'use strict';
/**
 * YAML parsing for profile.yaml files.
 *
 * Thin wrapper around `js-yaml` (a runtime dependency). The previous
 * hand-rolled parser was retired after a diff against js-yaml exposed two
 * latent bugs: it did not strip inline `#` comments (so `streaming: false # x`
 * became the string `"false # x"` and the runner treated streaming as on),
 * and it rendered an empty `env:` value as `""` instead of `null`.
 *
 * `parseYamlSimple` is kept as an alias for backwards compatibility with
 * callers that imported it by that name.
 */

const yaml = require('js-yaml');

function parseYaml(text) {
  // js-yaml handles JSON input too (JSON is a YAML subset), so the explicit
  // JSON.parse fast-path the hand-rolled parser had is unnecessary.
  return yaml.load(text);
}

module.exports = { parseYaml, parseYamlSimple: parseYaml };
