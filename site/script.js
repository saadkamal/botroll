(function () {
  var cfg = window.BOTROLL || {};
  var root = document.documentElement;
  var key = "botroll-theme";

  function apply(theme) {
    if (theme === "light" || theme === "dark") {
      root.setAttribute("data-theme", theme);
      root.style.colorScheme = theme;
    } else {
      root.removeAttribute("data-theme");
      root.style.colorScheme = "light dark";
      theme = "system";
    }
    document.querySelectorAll("[data-theme-set]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", btn.getAttribute("data-theme-set") === theme ? "true" : "false");
    });
  }

  var fromQuery = new URLSearchParams(location.search).get("theme");
  apply(fromQuery || localStorage.getItem(key));

  document.querySelectorAll("[data-theme-set]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = btn.getAttribute("data-theme-set");
      if (next === "system") localStorage.removeItem(key);
      else localStorage.setItem(key, next);
      apply(next);
    });
  });

  var themeBar = document.querySelector(".theme");
  if (themeBar) themeBar.hidden = false;

  if (cfg.github) {
    document.querySelectorAll("[data-github]").forEach(function (a) {
      a.setAttribute("href", cfg.github);
      a.hidden = false;
    });
    document.querySelectorAll("[data-open-source]").forEach(function (el) {
      el.hidden = false;
    });
    var ld = document.querySelector('script[type="application/ld+json"]');
    if (ld) {
      try {
        var data = JSON.parse(ld.textContent);
        var graph = data["@graph"] || [];
        var app = graph.filter(function (n) { return n["@type"] === "SoftwareApplication"; })[0];
        if (app) app.sameAs = cfg.github;
        graph.push({"@type": "SoftwareSourceCode", "name": "BotRoll", "codeRepository": cfg.github, "url": cfg.github});
        data["@graph"] = graph;
        ld.textContent = JSON.stringify(data);
      } catch (e) {}
    }
  } else {
    document.querySelectorAll("[data-github]").forEach(function (a) {
      a.removeAttribute("href");
      a.hidden = true;
    });
    document.querySelectorAll("[data-open-source]").forEach(function (el) {
      el.hidden = true;
    });
  }
  if (cfg.email) {
    document.querySelectorAll("[data-email]").forEach(function (a) {
      a.setAttribute("href", "mailto:" + cfg.email);
      if (!a.textContent.trim()) a.textContent = cfg.email;
    });
  }
})();
