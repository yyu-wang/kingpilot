(function () {
  'use strict';

  var css = getComputedStyle(document.documentElement);
  var v = function (name) {
    return css.getPropertyValue(name).trim();
  };

  if (window.mermaid) {
    window.mermaid.initialize({
      startOnLoad: true,
      securityLevel: 'loose',
      theme: 'base',
      themeVariables: {
        darkMode: true,
        background: v('--bg2'),
        fontFamily: "'Instrument Sans', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
        fontSize: '14px',
        primaryColor: v('--surface'),
        primaryTextColor: v('--ink'),
        primaryBorderColor: v('--rule'),
        secondaryColor: v('--surface-2'),
        secondaryTextColor: v('--ink'),
        secondaryBorderColor: v('--rule'),
        tertiaryColor: v('--bg2'),
        tertiaryTextColor: v('--ink'),
        tertiaryBorderColor: v('--rule'),
        mainBkg: v('--surface'),
        nodeBorder: v('--rule'),
        nodeTextColor: v('--ink'),
        lineColor: v('--diagram-line'),
        defaultLinkColor: v('--diagram-line'),
        textColor: v('--ink'),
        titleColor: v('--ink'),
        edgeLabelBackground: v('--bg2'),
        clusterBkg: v('--bg2'),
        clusterBorder: v('--rule'),
        actorBkg: v('--surface'),
        actorBorder: v('--accent'),
        actorTextColor: v('--ink'),
        actorLineColor: v('--rule'),
        signalColor: v('--diagram-line'),
        signalTextColor: v('--ink'),
        labelBoxBkgColor: v('--surface'),
        labelBoxBorderColor: v('--rule'),
        labelTextColor: v('--ink'),
        loopTextColor: v('--ink'),
        noteBkgColor: v('--accent-soft'),
        noteTextColor: v('--ink'),
        noteBorderColor: v('--accent'),
        activationBkgColor: v('--accent-soft'),
        activationBorderColor: v('--accent'),
        sequenceNumberColor: v('--bg')
      },
      flowchart: {
        curve: 'basis',
        htmlLabels: true,
        nodeSpacing: 30,
        rankSpacing: 46,
        padding: 12,
        useMaxWidth: true
      },
      sequence: {
        mirrorActors: false,
        actorMargin: 34,
        messageMargin: 30,
        noteMargin: 10,
        boxMargin: 8,
        useMaxWidth: true
      }
    });
  }

  var links = Array.prototype.slice.call(document.querySelectorAll('#sideNav a'));
  var sections = links
    .map(function (link) {
      var id = link.getAttribute('href').slice(1);
      return document.getElementById(id);
    })
    .filter(Boolean);

  function setActive(id) {
    links.forEach(function (link) {
      link.classList.toggle('active', link.getAttribute('href') === '#' + id);
    });
  }

  setActive('overview');

  var pkgToggle = document.getElementById('togglePkgs');
  var pkgDetails = Array.prototype.slice.call(document.querySelectorAll('details.pkg'));

  function syncPkgButton() {
    if (!pkgToggle || !pkgDetails.length) return;
    var allOpen = pkgDetails.every(function (item) { return item.open; });
    pkgToggle.textContent = allOpen ? '全部收起' : '全部展开';
  }

  if (pkgToggle && pkgDetails.length) {
    pkgToggle.addEventListener('click', function () {
      var anyClosed = pkgDetails.some(function (item) { return !item.open; });
      pkgDetails.forEach(function (item) { item.open = anyClosed; });
      syncPkgButton();
    });
    pkgDetails.forEach(function (item) {
      item.addEventListener('toggle', syncPkgButton);
    });
    syncPkgButton();
  }

  if ('IntersectionObserver' in window && sections.length) {
    var observer = new IntersectionObserver(
      function (entries) {
        var visible = entries
          .filter(function (entry) { return entry.isIntersecting; })
          .sort(function (a, b) { return a.boundingClientRect.top - b.boundingClientRect.top; });
        if (visible.length) {
          setActive(visible[0].target.id);
        }
      },
      { rootMargin: '-12% 0px -72% 0px', threshold: 0 }
    );
    sections.forEach(function (section) { observer.observe(section); });
  }
})();
