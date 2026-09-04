(function () {
  var form = document.getElementById("list-editor");
  if (!form) return;
  var body = document.getElementById("list-body");
  var save = document.getElementById("list-save");
  var cancel = document.getElementById("list-cancel");
  var search = document.getElementById("list-search");
  var searchClear = document.getElementById("list-search-clear");
  var searchStatus = document.getElementById("list-search-status");
  var hl = document.getElementById("list-body-hl");
  var scope = document.getElementById("scope-key");
  var snapshot = body.value;
  var dirty = false;

  function setDirty(next) {
    dirty = next;
    save.disabled = !dirty;
    cancel.disabled = !dirty;
  }

  function isDirty() {
    return body.value !== snapshot;
  }

  body.addEventListener("input", function () {
    setDirty(isDirty());
    runSearch();
  });
  body.addEventListener("scroll", syncHighlightScroll);

  cancel.addEventListener("click", function () {
    body.value = snapshot;
    setDirty(false);
    runSearch();
  });

  // Save is intentional navigation (PRG redirect). Clear dirty so the
  // browser does not treat the submit as abandoning unsaved edits.
  form.addEventListener("submit", function () {
    setDirty(false);
  });

  window.addEventListener("beforeunload", function (ev) {
    if (!dirty) return;
    ev.preventDefault();
    ev.returnValue = "";
  });

  function warn() {
    if (!dirty) return true;
    return window.confirm("You have unsaved list changes. Leave anyway?");
  }

  document.querySelectorAll("nav a").forEach(function (a) {
    a.addEventListener("click", function (ev) {
      if (!warn()) ev.preventDefault();
    });
  });

  function navigateKindOrScope() {
    var kind = (form.querySelector('input[name="kind"]:checked') || {}).value || "allow";
    var url = form.action + "?scope=" + encodeURIComponent(scope.value) + "&kind=" + encodeURIComponent(kind);
    window.location.assign(url);
  }

  scope.addEventListener("change", function (ev) {
    if (!warn()) {
      ev.preventDefault();
      scope.value = new URLSearchParams(window.location.search).get("scope") || scope.options[0].value;
      return;
    }
    setDirty(false);
    navigateKindOrScope();
  });

  form.querySelectorAll('input[name="kind"]').forEach(function (radio) {
    radio.addEventListener("change", function () {
      if (!warn()) {
        form.querySelector('input[name="kind"][value="' + (new URLSearchParams(window.location.search).get("kind") || "allow") + '"]').checked = true;
        return;
      }
      setDirty(false);
      navigateKindOrScope();
    });
  });

  function gotoLine(n) {
    var lines = body.value.split("\n");
    var pos = 0;
    for (var i = 0; i < n - 1 && i < lines.length; i++) {
      pos += lines[i].length + 1;
    }
    body.focus();
    body.setSelectionRange(pos, pos + (lines[n - 1] || "").length);
  }

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function syncHighlightScroll() {
    if (!hl) return;
    hl.scrollTop = body.scrollTop;
    hl.scrollLeft = body.scrollLeft;
  }

  function paintHits(hitIdx) {
    if (!hl) return;
    if (!hitIdx || !hitIdx.length) {
      hl.innerHTML = "";
      return;
    }
    var marked = {};
    for (var i = 0; i < hitIdx.length; i++) marked[hitIdx[i]] = true;
    var lines = body.value.split("\n");
    var html = "";
    for (var j = 0; j < lines.length; j++) {
      var line = esc(lines[j]);
      html += marked[j] ? "<mark>" + line + "</mark>" : line;
      if (j < lines.length - 1) html += "\n";
    }
    hl.innerHTML = html;
    syncHighlightScroll();
  }

  var errLine = body.getAttribute("data-error-line");
  if (errLine) {
    gotoLine(parseInt(errLine, 10));
    setDirty(true);
  }

  function runSearch() {
    var q = (search.value || "").toLowerCase();
    if (!q) {
      searchStatus.textContent = "";
      paintHits([]);
      return;
    }
    var lines = body.value.split("\n");
    var hits = [];
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].toLowerCase().indexOf(q) !== -1) hits.push(i);
    }
    if (!hits.length) {
      searchStatus.textContent = "no matches";
      paintHits([]);
      return;
    }
    searchStatus.textContent = hits.length + " match" + (hits.length === 1 ? "" : "es");
    paintHits(hits);
  }

  search.addEventListener("input", runSearch);
  searchClear.addEventListener("click", function () {
    search.value = "";
    searchStatus.textContent = "";
    paintHits([]);
    search.focus();
  });
})();
