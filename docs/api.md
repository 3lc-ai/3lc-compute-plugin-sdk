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
