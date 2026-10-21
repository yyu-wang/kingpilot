(function () {
  var body = document.body;
  var toggle = document.getElementById('toggleAnswers');

  if (toggle) {
    toggle.setAttribute('aria-pressed', 'false');
    toggle.addEventListener('click', function () {
      var hidden = body.classList.toggle('hide-answers');
      toggle.textContent = hidden ? '显示全部答案' : '隐藏全部答案';
      toggle.setAttribute('aria-pressed', hidden ? 'true' : 'false');
    });
  }

  var links = Array.prototype.slice.call(document.querySelectorAll('.toc a[href^="#"]'));
  var sections = links
    .map(function (link) {
      var id = link.getAttribute('href').slice(1);
      return { link: link, section: document.getElementById(id) };
    })
    .filter(function (item) { return item.section; });

  if ('IntersectionObserver' in window && sections.length) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        sections.forEach(function (item) {
          item.link.classList.toggle('active', item.section === entry.target);
        });
      });
    }, { rootMargin: '-20% 0px -70% 0px', threshold: 0 });

    sections.forEach(function (item) { observer.observe(item.section); });
  }
})();
