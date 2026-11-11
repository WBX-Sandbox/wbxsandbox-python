# wbxsandbox – Python SDK for WBX Sandbox

Python SDK for [WBX Sandbox](https://sandbox.wbx.cz): isolated cloud sandboxes for AI agents
and code execution (Firecracker microVMs). No dependencies, Python 3.9+.

```bash
pip install wbxsandbox
```

Usage, configuration and examples: [`sdk/python/README.md`](sdk/python/README.md) ·
Documentation: https://sandbox.wbx.cz/docs · Pricing: https://sandbox.wbx.cz/pricing

## About this repository

The SDK is developed together with the WBX Sandbox platform. This repository holds the published
copy of `sdk/python` from a platform release (see [`RELEASE`](RELEASE)) and publishes it to PyPI.
The SDK's `major.minor` follows the platform release line, so `RELEASE` also lets the version test
check that the two match.

Releases: push a tag `vX.Y.Z` equal to `version` in `sdk/python/pyproject.toml`; the Release
workflow runs the tests, builds the package and publishes it to PyPI with trusted publishing.

## License

MIT – see [LICENSE](LICENSE).
