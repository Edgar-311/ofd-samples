# AGENTS.md

Instructions for AI coding agents (Claude, Codex and others) working on this repository.
Read this file first; it carries the context between sessions.

## What this project is

ofd-samples is a public set of synthetic OFD test files (GB/T 33190) with expected outputs,
so that OFD libraries in any language can test against the same files. It is the companion
of [ofdkit](https://github.com/Edgar-311/ofdkit), but must stay useful without it.

Layout:

- `samples/<name>.ofd`: the test files
- `expected/<name>.json`: expected structure and per-page text (format in README.md)
- `generator/build_samples.py`: builds every sample and expected output deterministically
  from a scene description. Never edit samples/, expected/ or CATALOG.md by hand: change
  the generator, run it, commit the result. `--check` verifies the committed files.
- `generator/validate.py`: checks that do not rely on the generator's own code (strict XML
  parsing of conforming samples, expected-output fields).
- `CATALOG.md`: generated list of samples and what each exercises.

Adding a sample: add a `Sample(...)` in `samples()` with a description, `conforming`,
`features` and the expected text of each page written by hand (not produced by a reader).
Expected coordinates come from the scene; keep them independent of any OFD library.
Then run ofdkit's `tests/test_samples.py` against it.

## Rules that are never relaxed

1. Plain documents only. Never create a file that looks like an invoice, a receipt or any
   other official record, and never imitate a real issuer, seal or format. Samples that
   exercise invoice-style custom tags still show ordinary text such as "测试样例".
2. No real data: no real names, companies, tax numbers or addresses. Do not add files that
   others send in; rebuild the structure they need synthetically.
3. Everything here is CC0-1.0, so only add material we created ourselves.
4. Be honest about AI involvement. The README says the content is AI-assisted; keep that
   note. Commits name only the maintainer, who directs and owns the work (see Commits).

## Commits

- Author and committer: only the maintainer, `Edgar-311
  <347874636+Edgar-311@users.noreply.github.com>`, GitHub's no-reply address. No
  `Co-Authored-By` trailer for agents: the maintainer decided on 2026-10-28 that commits
  name only them (the README keeps the AI-assisted note). Never use a personal email
  address. In a fresh clone, set this once:
  `git config user.name Edgar-311`
  `git config user.email 347874636+Edgar-311@users.noreply.github.com`
- Push reviewed commits to main yourself instead of using GitHub's merge or squash
  button or API, which write the account's primary email address.
- Never rewrite or force-push published history.

## Working with the maintainer

The maintainer is Edgar-311, the repository owner. Write to them in simple Chinese:
short sentences, and explain technical terms the first time they appear. Ask before
anything public or irreversible; otherwise make sensible decisions and report them.

## Status

- [x] Repository and license (CC0-1.0)
- [x] Generator, 22 samples, expected outputs, catalog, CI (`check.yml`)

Ideas for more samples: annotations (Annots) carrying text, multiple DocBody entries,
vertical text, embedded fonts, clip paths, signatures, PageRes resources, DeltaY.
