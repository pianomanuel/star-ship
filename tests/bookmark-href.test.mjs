import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const src = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../site/bookmark-manager.js"),
  "utf8",
);
const start = src.indexOf("function esc(");
const end = src.indexOf("function msg(");
if (start < 0 || end < start) {
  throw new Error("bookmark href helpers moved; update this test");
}
const { esc, safeHref } = new Function(`${src.slice(start, end)}; return { esc, safeHref };`)();

test("bookmark text is escaped before it is inserted as HTML", () => {
  assert.equal(esc(null), "");
  assert.equal(esc(`a&b<c>d"e'f`), "a&amp;b&lt;c&gt;d&quot;e&#39;f");
});

test("bookmark hrefs keep navigable schemes and neutralize scripts", () => {
  assert.equal(safeHref("https://example.com/a"), "https://example.com/a");
  assert.equal(safeHref("HTTP://example.com"), "HTTP://example.com");
  assert.equal(safeHref("file:///tmp/a"), "file:///tmp/a");
  assert.equal(safeHref("mailto:a@b.c"), "mailto:a@b.c");
  assert.equal(safeHref("ftp://files.example/a"), "ftp://files.example/a");
  for (const blocked of ["javascript:alert(1)", "data:text/html,x", "smb://share/a", " https://example.com", "", null]) {
    assert.equal(safeHref(blocked), "#", String(blocked));
  }
  const hostile = `https://example.com/?q="><script>alert(1)</script>`;
  const href = esc(safeHref(hostile));
  assert.equal(href.includes("<"), false);
  assert.equal(href.includes('"'), false);
  assert.match(href, /^https:/);
});
