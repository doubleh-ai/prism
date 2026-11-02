(function () {
  'use strict';
  try {
    var theme = localStorage.getItem('cl-theme');
    if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  } catch (error) { /* System preference remains available. */ }
  var products = document.getElementById('products');
  var notice = document.getElementById('notice');
  var noticeText = document.getElementById('noticeText');
  var account = document.getElementById('account');
  var destinations = {prism:'/prism', work:'/work'};

  function setProduct(id, available) {
    var old = products.querySelector('[data-product="' + id + '"]');
    var item = document.createElement(available ? 'a' : 'div');
    item.className = 'product';
    item.dataset.product = id;
    if (available) item.href = destinations[id];
    while (old.firstChild) item.appendChild(old.firstChild);
    var state = item.querySelector('.product-state, .product-arrow');
    state.className = available ? 'product-arrow' : 'product-state';
    state.textContent = available ? '↗' : '暂不可用';
    if (available) state.setAttribute('aria-hidden', 'true');
    else state.removeAttribute('aria-hidden');
    old.replaceWith(item);
  }
  async function load() {
    notice.hidden = true;
    products.setAttribute('aria-busy', 'true');
    try {
      var responses = await Promise.all(['/api/me', '/api/platform'].map(function (path) {
        return fetch(path, {credentials:'same-origin', cache:'no-store'});
      }));
      if (responses.some(function (response) { return response.status === 401; })) {
        window.location.replace('/login');
        return;
      }
      if (responses.some(function (response) { return !response.ok; })) throw new Error('request failed');
      var values = await Promise.all(responses.map(function (response) { return response.json(); }));
      if (!Array.isArray(values[1].products)) throw new Error('invalid response');
      account.textContent = typeof values[0].user === 'string' ? values[0].user : '';
      Object.keys(destinations).forEach(function (id) {
        var product = values[1].products.find(function (entry) { return entry.id === id; });
        setProduct(id, !!product && product.available === true);
      });
    } catch (error) {
      Object.keys(destinations).forEach(function (id) { setProduct(id, false); });
      noticeText.textContent = '无法读取产品入口，请重试。';
      notice.hidden = false;
    } finally {
      products.removeAttribute('aria-busy');
    }
  }
  document.getElementById('retry').addEventListener('click', load);
  load();
}());
