# CLAUDE.md

- Always use the "validation" field for specifying the testing conditions.
- A `validation` is a Python expression, and it names the values it tests in
  braces: `"{count} == 192"`, `'matches({line}, r"unix=[0-9]+")'`. A bare name
  is rejected when the scenario loads.
- Each tag offers its own variables; they are listed in that tag's wrapper
  docstring under `wrappers/`, and in README.md section 3.
- Quote a regular expression single-outside, raw-inside:
  `validation: 'matches({line}, r"\S+")'`. Double-quoted YAML breaks on the
  backslash, and a non-raw Python string breaks on the escape.
