(function () {
  console.log("[login.js] script loaded");

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn, { once: true });
    } else {
      fn();
    }
  }

  onReady(function () {
    const form = document.getElementById("login-form");
    const errBox = document.getElementById("login-error");
    const btn = document.getElementById("login-btn");
    const usernameEl = document.getElementById("username");
    const passwordEl = document.getElementById("password");

    if (!form || !errBox || !btn || !usernameEl || !passwordEl) {
      console.error("[login.js] missing nodes", {
        form: !!form, errBox: !!errBox, btn: !!btn,
        usernameEl: !!usernameEl, passwordEl: !!passwordEl,
      });
      return;
    }
    console.log("[login.js] DOM ready");

    fetch("/api/auth/me", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && data.data) {
          console.log("[login.js] already authed, jump /");
          location.href = "/";
        }
      })
      .catch((e) => console.warn("[login.js] me probe failed:", e));

    async function doLogin(ev) {
      if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
      if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
      console.log("[login.js] doLogin start");

      errBox.style.display = "none";
      btn.disabled = true;
      btn.textContent = "登录中…";
      try {
        const username = usernameEl.value.trim();
        const password = passwordEl.value;
        if (!username || !password) {
          errBox.textContent = "请输入用户名和密码";
          errBox.style.display = "block";
          return;
        }
        console.log("[login.js] POST /api/auth/login");
        const resp = await fetch("/api/auth/login", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password }),
        });
        console.log("[login.js] status:", resp.status);
        const payload = await resp.json().catch(() => ({}));
        console.log("[login.js] body:", payload);
        if (resp.ok && payload && payload.data) {
          console.log("[login.js] ok, jump /");
          location.href = "/";
          return;
        }
        errBox.textContent = (payload && payload.error) || "登录失败";
        errBox.style.display = "block";
      } catch (e) {
        console.error("[login.js] fetch error:", e);
        errBox.textContent = "网络错误：" + (e && e.message);
        errBox.style.display = "block";
      } finally {
        btn.disabled = false;
        btn.textContent = "登录";
      }
      return false;
    }

    // 三重保险：form submit + button click + Enter 键
    form.addEventListener("submit", doLogin);
    btn.addEventListener("click", doLogin);
    [usernameEl, passwordEl].forEach((el) => {
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter") doLogin(e);
      });
    });

    console.log("[login.js] handlers bound");
  });
})();
