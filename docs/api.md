# API reference

The public surface of `tlc_plugin_sdk`. Everything below is generated from the
package's own docstrings.

## The contract

```{eval-rst}
.. autoclass:: tlc_plugin_sdk.ComputePlugin
   :members:

.. autoclass:: tlc_plugin_sdk.JobContext
   :members:

.. autodata:: tlc_plugin_sdk.SDK_CONTRACT_VERSION

.. autodata:: tlc_plugin_sdk.PY_CONTRACT

.. autodata:: tlc_plugin_sdk.JS_CONTRACT
```

`SDK_CONTRACT_VERSION` is the wheel/SemVer — the dependency *pin* a plugin resolves
against (`3lc-compute-plugin-sdk>=X,<Y`). `PY_CONTRACT` and `JS_CONTRACT` are finer-grained
**capability markers** for runtime feature-detection: `PY_CONTRACT` covers the Python
surface (`ComputePlugin` / `JobContext`), `JS_CONTRACT` the browser surface
(`PLUGIN_API` / `PluginJobs` / `TlcData`, declared in
`tlc_plugin_sdk/contract/plugin-api.d.ts`). Both increment independently and are always
`<=` the package version. See `docs/plugin-guide.md` → "Two version axes".

The `.d.ts` ships in the wheel at
`<site-packages>/tlc_plugin_sdk/contract/plugin-api.d.ts`; a plain-JS `ui.html` references
it with `/// <reference types="3lc-plugin-sdk/contract/plugin-api" />`.

## The browser contract (`JS_CONTRACT`)

The browser surface a plugin's `ui.html` programs against — `PLUGIN_API`, `PluginJobs`,
and the `TlcData` helper — is declared in `plugin-api.d.ts`, the source of truth for
`JS_CONTRACT`.

```{eval-rst}
.. automodule documents every exported symbol in the file, so new contract
   interfaces appear here automatically. The module key is the source filename
   with TypeDoc's trailing ``.ts`` stripped: ``plugin-api.d.ts`` -> ``plugin-api.d``.
.. js:automodule:: plugin-api.d
```

## The worker

```{eval-rst}
.. automodule:: tlc_plugin_sdk.worker
   :members:
```

## Shared utilities

Plugin-facing helpers that depend only on `tlc` + the standard library — staged
here so out-of-process (venv) plugin workers can import them without pulling in
the service.

```{eval-rst}
.. automodule:: tlc_plugin_sdk.shared.aliases
   :members:

.. automodule:: tlc_plugin_sdk.shared.config_store
   :members:

.. automodule:: tlc_plugin_sdk.shared.generic_job
   :members:

.. automodule:: tlc_plugin_sdk.shared.images
   :members:

.. automodule:: tlc_plugin_sdk.shared.labels
   :members:

.. automodule:: tlc_plugin_sdk.shared.modality
   :members:

.. automodule:: tlc_plugin_sdk.shared.model_storage
   :members:

.. automodule:: tlc_plugin_sdk.shared.naming
   :members:

.. automodule:: tlc_plugin_sdk.shared.url_utils
   :members:
```
