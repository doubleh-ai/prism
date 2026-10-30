(function () {
  'use strict';
  var status = document.getElementById('recoveryStatus');
  var send = document.getElementById('sendCode');
  var form = document.getElementById('resetForm');
  var reset = document.getElementById('resetPassword');
  var code = document.getElementById('code');
  var password = document.getElementById('newPassword');
  var confirm = document.getElementById('confirmPassword');
  var challenge = '', busy = false, timer;
  async function post(path, body) {
    var response = await fetch(path, {
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(body)
    });
    var data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作未完成，请稍后重试');
    return data;
  }
  send.addEventListener('click', async function () {
    if (busy || send.disabled) return;
    busy = true; send.disabled = true; reset.disabled = true;
    challenge = ''; form.hidden = true; code.value = ''; password.value = ''; confirm.value = '';
    status.textContent = '正在请求发送验证码…';
    try {
      var data = await post('/api/auth/recovery/request', {});
      challenge = data.challenge_id; form.hidden = false;
      status.textContent = data.detail; code.focus();
    } catch (error) {
      status.textContent = error instanceof TypeError ? '网络连接中断，发送结果未确认；稍后可手动重试。' : error.message;
    } finally {
      busy = false; reset.disabled = false;
      // Never automatically resend when the delivery outcome is unknown.
      send.textContent = '60 秒后可重新申请';
      clearTimeout(timer);
      timer = setTimeout(function () {
        send.disabled = false; send.textContent = '重新申请验证码';
      }, 60000);
    }
  });
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (busy || !challenge) return;
    if (password.value !== confirm.value || new TextEncoder().encode(password.value).length > 72) {
      status.textContent = '请确认两次密码一致，且不超过 72 字节。'; return;
    }
    busy = true; reset.disabled = true; send.disabled = true;
    var previousChallenge = challenge;
    try {
      var data = await post('/api/auth/recovery/reset', {
        challenge_id: previousChallenge, code: code.value,
        password: password.value, confirm_password: confirm.value
      });
      challenge = ''; clearTimeout(timer); form.hidden = true; send.hidden = true;
      status.textContent = data.detail;
    } catch (error) {
      status.textContent = error instanceof TypeError ? '网络连接中断，重置结果未确认。请先尝试使用新密码登录，勿自动重试。' : error.message;
    } finally {
      password.value = ''; confirm.value = ''; code.value = '';
      busy = false; reset.disabled = false;
      if (challenge) {
        clearTimeout(timer);
        timer = setTimeout(function () { send.disabled = false; send.textContent = '重新申请验证码'; }, 60000);
      }
    }
  });
  fetch('/api/auth/recovery/status', {cache: 'no-store'}).then(function (response) {
    if (!response.ok) throw new Error('unavailable');
    return response.json();
  }).then(function (data) {
    send.disabled = !data.configured;
    status.textContent = data.configured
      ? '验证码仅发送至预先绑定的管理员邮箱。点击后请到邮箱查收。'
      : '管理员邮箱找回尚未启用。需要平台维护者先配置收信邮箱和发信服务。';
  }).catch(function () { status.textContent = '暂时无法检查找回服务，请稍后刷新重试。'; });
}());
