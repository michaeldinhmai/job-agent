// XSS guard regression tests. Run: node test_xss.js
//
// escapeHtml and safeHref are the only things standing between listing text
// (which comes from third-party job boards, not from us) and the DOM. They
// are four lines each and trivially easy to "simplify" back into a hole, so
// they get tested against the real source rather than a copy: the functions
// are extracted out of static/app.js at run time.

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "jobagent", "static", "app.js"), "utf8");

function extract(name) {
  // Both helpers are top-level declarations closed by a brace in column 0.
  const re = new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`);
  const m = src.match(re);
  if (!m) throw new Error(`could not find ${name}() in static/app.js`);
  return m[0];
}

// safeHref resolves relative URLs against window.location.origin.
const window = { location: { origin: "http://127.0.0.1:5151" } };
const { escapeHtml, safeHref } = new Function(
  "window",
  `${extract("escapeHtml")}\n${extract("safeHref")}\nreturn { escapeHtml, safeHref };`
)(window);

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`ok   ${name}`);
  } else {
    failures++;
    console.log(`FAIL ${name}${detail ? `\n       ${detail}` : ""}`);
  }
}

// ---------- escapeHtml ----------
const script = escapeHtml('<script>alert(1)</script>');
check("escapeHtml neutralizes tags", !script.includes("<") && !script.includes(">"), script);
check("escapeHtml escapes double quotes", escapeHtml('a"b') === "a&quot;b", escapeHtml('a"b'));
check("escapeHtml escapes single quotes", escapeHtml("a'b") === "a&#39;b", escapeHtml("a'b"));
check("escapeHtml escapes ampersands", escapeHtml("a&b") === "a&amp;b", escapeHtml("a&b"));
// & must be escaped FIRST, or the escapes themselves get double-decoded.
check("escapeHtml escapes & before the others",
  escapeHtml("&lt;") === "&amp;lt;", escapeHtml("&lt;"));
check("escapeHtml handles null/undefined",
  escapeHtml(null) === "" && escapeHtml(undefined) === "");
// An attribute-breaking payload: title="..." must stay one attribute.
const attr = escapeHtml('" onmouseover="alert(1)');
check("escapeHtml blocks attribute break-out", !attr.includes('"'), attr);

// ---------- safeHref ----------
check("safeHref allows https", safeHref("https://example.com/x") === "https://example.com/x");
check("safeHref allows http", safeHref("http://example.com/x") === "http://example.com/x");
for (const evil of [
  "javascript:alert(1)",
  "JavaScript:alert(1)",
  "  javascript:alert(1)",
  "java\tscript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "vbscript:msgbox(1)",
  "file:///etc/passwd",
]) {
  check(`safeHref blocks ${JSON.stringify(evil)}`, safeHref(evil) === "#", safeHref(evil));
}
check("safeHref escapes the URL it does allow",
  !safeHref('https://example.com/"onmouseover="alert(1)').includes('"'));

console.log(failures ? `\n${failures} failed` : "\nall passed");
process.exit(failures ? 1 : 0);
