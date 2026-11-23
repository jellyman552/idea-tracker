// Theme toggle (initial theme is set inline in <head>).
(function () {
  var btn = document.getElementById("themeBtn");
  if (!btn) return;
  function label() {
    btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀︎" : "☾";
  }
  label();
  btn.addEventListener("click", function () {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) {}
    label();
  });
})();

// Selects and checkboxes marked data-autosubmit submit their form on change.
// Delegated, so it keeps working after live search swaps in a new toolbar.
document.addEventListener("change", function (e) {
  var el = e.target.closest("[data-autosubmit]");
  if (!el) return;
  // A new sort key should start in its own default direction.
  if (el.hasAttribute("data-reset-dir")) {
    var dir = el.form.querySelector("input[name=dir]");
    if (dir) dir.remove();
  }
  el.form.submit();
});

// Whole cards / table rows are clickable.
document.addEventListener("click", function (e) {
  var el = e.target.closest("[data-href]");
  if (!el || e.target.closest("a, button, input, form")) return;
  location.href = el.dataset.href;
});

// Live search: filter the dashboard as you type, without a page reload.
(function () {
  var form = document.querySelector("form[data-live]");
  if (!form) return;
  var input = form.querySelector("input[name=q]");
  var timer = null;
  var latest = 0;

  function refresh() {
    var params = new URLSearchParams(new FormData(form));
    if (!params.get("q")) params.delete("q");
    var url = location.pathname + "?" + params.toString();
    var seq = ++latest;
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        if (seq !== latest) return; // a newer keystroke already won
        var doc = new DOMParser().parseFromString(html, "text/html");
        var results = doc.getElementById("results");
        var toolbar = doc.getElementById("toolbar");
        if (!results || !toolbar) { location.href = url; return; }
        document.getElementById("results").replaceWith(results);
        // Swap the rest of the toolbar (its links carry the new query) but keep
        // the live input element so focus and caret position survive.
        var caret = input.selectionStart;
        toolbar.querySelector("input[name=q]").replaceWith(input);
        form.replaceWith(toolbar);
        form = toolbar;
        input.focus();
        try { input.setSelectionRange(caret, caret); } catch (e) {}
        history.replaceState(null, "", url);
      })
      .catch(function () { location.href = url; });
  }

  // On the input itself: it is the one element kept when the toolbar is swapped.
  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(refresh, 250);
  });
  // Enter (or clearing the search box) updates immediately instead of reloading.
  document.addEventListener("submit", function (e) {
    if (e.target !== form) return;
    e.preventDefault();
    clearTimeout(timer);
    refresh();
  });
})();

// Archive forms confirm in a dialog first.
(function () {
  var dlg = document.getElementById("confirmDlg");
  var pending = null;
  document.querySelectorAll("form[data-confirm-title]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (form.dataset.confirmed) return;
      e.preventDefault();
      pending = form;
      document.getElementById("confirmTitle").textContent = form.dataset.confirmTitle;
      document.getElementById("confirmBody").textContent = form.dataset.confirmBody;
      dlg.showModal();
    });
  });
  document.getElementById("confirmCancel").addEventListener("click", function () { dlg.close(); pending = null; });
  document.getElementById("confirmOk").addEventListener("click", function () {
    dlg.close();
    if (pending) { pending.dataset.confirmed = "1"; pending.submit(); }
  });
})();
