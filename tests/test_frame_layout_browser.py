# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Real browser layout against the packaged runtime and a cross-origin sizing host.

Run with ``uv run --with playwright pytest tests/test_frame_layout_browser.py``.
The browser CI job installs Chromium explicitly; a missing browser is a failure.
"""

import json
from collections.abc import Iterator

import pytest

from tlc_plugin_sdk.shared.frame_runtime import frame_runtime_script

playwright = pytest.importorskip("playwright.sync_api")

_HOST = """<!doctype html><style>iframe{width:600px;height:400px;border:0}</style>
<iframe src="https://compute.test/frame?hub=https://hub.test"></iframe>
<script>
window.heights = [];
addEventListener('message', event => {
  if (event.origin !== 'https://compute.test' || event.data.type !== 'ready') return;
  const channel = new MessageChannel();
  channel.port1.onmessage = event => {
    if (event.data.type !== 'height') return;
    heights.push(event.data.value);
    document.querySelector('iframe').style.height = Math.max(120, event.data.value) + 'px';
  };
  event.source.postMessage({v:1, type:'init', pluginId:'test', layout:LAYOUT,
    html:'<div id="content" style="height:200px"></div>', config:{}}, event.origin, [channel.port2]);
});
</script>"""

_FRAME = """<!doctype html><style>
html,body{margin:0;background:transparent}html body{display:block;min-height:0}
#tlc-plugin-root{padding:0}
</style><script src="/frame.js"></script><div id="tlc-plugin-root"></div>"""


@pytest.fixture
def browser() -> Iterator[playwright.Browser]:
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        yield browser
        browser.close()


def _mount(browser: playwright.Browser, layout: str = "flow") -> tuple[playwright.Page, playwright.Frame]:
    page = browser.new_page()
    page.route(
        "https://hub.test/**",
        lambda route: route.fulfill(content_type="text/html", body=_HOST.replace("LAYOUT", json.dumps(layout))),
    )
    page.route(
        "https://compute.test/**",
        lambda route: route.fulfill(
            content_type="text/javascript" if route.request.url.endswith("frame.js") else "text/html",
            body=frame_runtime_script() if route.request.url.endswith("frame.js") else _FRAME,
        ),
    )
    page.goto("https://hub.test/")
    frame = next(frame for frame in page.frames if frame.url.startswith("https://compute.test/"))
    frame.wait_for_selector("#content")
    return page, frame


def _height(page: playwright.Page, expected: int) -> None:
    page.wait_for_function("height => document.querySelector('iframe').clientHeight === height", arg=expected)


@pytest.mark.parametrize(
    "content",
    [
        '<div style="height:1400px"></div>',
        '<div style="height:1300px;margin:40px 0 60px"></div>',
        '<div style="height:1400px;float:left;width:50px"></div>',
    ],
)
def test_flow_grows_and_shrinks_including_margins_and_floats(browser: playwright.Browser, content: str) -> None:
    page, frame = _mount(browser)
    _height(page, 200)
    frame.locator("#content").evaluate("(el, html) => {el.style.height = ''; el.innerHTML = html;}", content)
    _height(page, 1400)
    frame.locator("#content").evaluate("el => {el.innerHTML = ''; el.style.height = '200px';}")
    _height(page, 200)
    frame.locator("#content").evaluate("el => el.hidden = true")
    _height(page, 120)
    frame.locator("#content").evaluate("el => el.hidden = false")
    _height(page, 200)
    assert frame.evaluate("document.documentElement.scrollHeight") == 200
    page.close()


def test_flow_reflows_when_width_changes(browser: playwright.Browser) -> None:
    page, frame = _mount(browser)
    frame.locator("#content").evaluate("""el => {
      el.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))';
      el.innerHTML = '<div style="height:200px"></div><div style="height:200px"></div>';
    }""")
    _height(page, 200)
    page.locator("iframe").evaluate("el => el.style.width = '300px'")
    _height(page, 400)
    page.locator("iframe").evaluate("el => el.style.width = '600px'")
    _height(page, 200)
    page.close()


def test_fill_does_not_report_content_heights(browser: playwright.Browser) -> None:
    page, frame = _mount(browser, "fill")
    frame.locator("#content").evaluate("el => el.style.height = '1400px'")
    frame.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert page.evaluate("heights") == []
    assert page.locator("iframe").evaluate("el => el.clientHeight") == 400
    assert frame.locator("#tlc-plugin-root").evaluate("el => el.style.display") == ""
    page.close()
