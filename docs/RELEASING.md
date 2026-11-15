# Release packaging

Repository name: `thearyanag/hermes-commitments`. License: MIT. Version 0.2.0 is
still an unreleased development build; packaging does not settle production
readiness or authorize calling unqualified features supported.

## Build and review

```sh
python3 scripts/package_release.py --output dist/release
```

The output directory must not exist. Choose a new directory for a later build.
The exporter never overwrites an old artifact. It reads only `release-files.txt`,
rejects missing files, symlinks and non-text payloads, and writes:

- `source/`: the exact public repository candidate, including tests and CI.
- `hermes-commitments-0.2.0-source.tar.gz`: that source tree, reproducibly packed.
- `commitments-0.2.0.tar.gz`: the installable plugin with standalone README/license.
- `source-manifest.json`: SHA-256 for every exported source file.
- `SHA256SUMS`: checksums for the archives and manifest.

No Git initialization, upload, installation, configuration change, or model call
is performed. The export excludes the local spec/research history, transcripts,
fixture archives, app builds, databases, provider configuration, and machine-only
installation/live-test helpers. Do not publish the surrounding research workspace.

Review the inventory and file contents for credentials and private data. Inspect
both archive listings and verify `SHA256SUMS` (`sha256sum -c SHA256SUMS` on Linux,
`shasum -a 256 -c SHA256SUMS` on macOS). Run all checks from
[CONTRIBUTING.md](../CONTRIBUTING.md) against `source/`, plus validation of an
extracted installable archive. Do not add generated caches to a commit.

## Publish when the release scope is settled

1. Finalize development/preview versus production labelling and changelog.
   Version changes must match the manifest, changelog, and catalog entry.
2. Commit only the exported source inventory to the public repository. The
   repository root must contain `plugin.yaml` and the complete plugin.
3. Run hosted CI on that exact commit. Local checks do not establish hosted CI
   success. Record the full commit SHA and keep release artifacts tied to it.
4. Attach the plugin/source archives, source manifest, and checksums to the
   release. Development releases must be marked prerelease on GitHub. A numeric
   manifest version alone does not communicate production qualification.
5. Once public, document installation by repository and immutable commit:

   ```sh
   hermes plugins install thearyanag/hermes-commitments --ref FULL_40_CHARACTER_COMMIT_SHA --no-enable
   ```

   Replace the placeholder with the actual published SHA. Then follow the
   plugin README to configure and enable both components.

## Catalog PR

Follow the official
[catalog submission requirements](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
Copy `catalog-entry.yaml.example` to `plugin-catalog/commitments.yaml` in a
separate upstream contribution checkout. Replace its placeholder with the exact
public, validated 40-character SHA. Do not mix this with an unrelated Hermes fix.

`catalog-pr.md` is a draft disclosure body. Fill in exact validation receipts and
sanitized screenshots of visible follow-up behavior before submitting. The
README at the repository root must remain self-contained because Hermes renders it
on the catalog page. A submitted PR is not catalog acceptance; installation by
bare catalog name should be documented only after maintainer merge.

No catalog PR or release is created by the packaging script or CI workflow.
