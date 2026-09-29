# ECharts GL CSP compatibility

The bundled ECharts GL 2.0.9 retains its upstream license. OptionHelper precompiles the 23 fixed viewport-size expressions shipped in its compositor into static functions. This avoids the upstream `new Function` expression compiler under the report preview CSP. Unknown expressions are rejected; report input cannot supply executable JavaScript. Rendering values, shaders and geometry are unchanged.

When upgrading the library, review all embedded `expr(...)` strings and compile their arithmetic into static functions. Do not enable `unsafe-eval`. The regression fixture and compilation recipe are in `tests/demo_sync_20260923/chart_failure`; the fixture uses the App's unchanged report CSP and compares the original five failing surfaces with five successful surfaces after replacement.
