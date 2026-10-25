"use strict";
const byId = id => document.getElementById(id);
const statusNode = byId("status");
let timer, currentState = "", phoneChosen = false, busy = false, resendAfter = null;
const terminal = ["connected", "wrong_account", "expired", "failed", "cancelled"];
const messages = {
  approval_required: "Ожидаем подтверждения в личном чате с ботом.",
  idle: "Открытие подтверждено. Выберите удобный способ входа.",
  waiting: "Отсканируйте QR-код со второго устройства и подтвердите вход в Telegram.",
  code_required: "Введите код только на этой защищённой странице.",
  password_required: "Telegram запросил пароль двухэтапной аутентификации.",
  connected: "Аккаунт успешно подключён. Вернитесь в бот и нажмите «Мои чаты».",
  wrong_account: "Аккаунт не совпадает с аккаунтом в боте. Новая сессия отозвана. Получите новую ссылку: /connect.",
  expired: "QR-код истёк. Получите новую ссылку в боте: /connect.",
  failed: "Вход прерван. Получите новую ссылку в боте: /connect.",
  cancelled: "Авторизация отменена. При необходимости получите новую ссылку в боте."
};
const csrf = () => document.cookie.split("; ").find(x => x.startsWith("deadline_csrf="))?.split("=")[1] || "";
async function api(path, body) {
  const response = await fetch(`/api/auth/${path}`, {
    method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()},
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(35000)
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(typeof result.detail === "string" ? result.detail : "Запрос не выполнен");
    error.status = response.status;
    throw error;
  }
  return result;
}
function showResend() {
  const available = currentState === "code_required" && resendAfter !== null;
  byId("resend").hidden = !available;
  byId("resend").disabled = busy || !available || resendAfter > 0;
  byId("resend-help").textContent = currentState !== "code_required" ? "" : !available
    ? "Повторный запрос для этого входа недоступен. Если код не пришёл, отмените вход и получите новую ссылку в боте для входа по QR со второго устройства."
    : resendAfter > 0
      ? `Повторный запрос будет доступен через ${resendAfter} с. Автоматически код не запрашивается.`
      : "Можно запросить следующий способ подтверждения, разрешённый Telegram. Доставка кода не гарантируется.";
}
function show(result) {
  const state = result.state;
  if (state !== currentState) statusNode.textContent = messages[state] || "Проверяем подключение…";
  if (state === "waiting" && currentState !== state) byId("qr").src = "/api/auth/qr.svg";
  currentState = state;
  resendAfter = result.resend_after ?? null;
  showResend();
  byId("approval").hidden = state !== "approval_required";
  byId("request-tag").textContent = result.request_tag || "";
  byId("methods").hidden = state !== "idle" || phoneChosen;
  byId("phone-form").hidden = state !== "idle" || !phoneChosen;
  byId("qr-panel").hidden = state !== "waiting";
  byId("code-form").hidden = state !== "code_required";
  byId("password-form").hidden = state !== "password_required";
  byId("delivery").textContent = result.delivery || "";
  byId("cancel").hidden = terminal.includes(state);
  if (terminal.includes(state)) {
    clearTimeout(timer);
    for (const input of document.querySelectorAll("input")) input.value = "";
    byId("qr").removeAttribute("src");
  }
}
async function refresh() {
  try { show(await api("state")); }
  catch (error) {
    statusNode.textContent = error.message;
    if (error.status === 401) {
      clearTimeout(timer);
      for (const section of document.querySelectorAll("section, form, #cancel")) section.hidden = true;
      return;
    }
  }
  if (!terminal.includes(currentState)) timer = setTimeout(refresh, 2000);
}
async function submit(path, body) {
  if (busy) return;
  busy = true;
  clearTimeout(timer);
  for (const button of document.querySelectorAll("button")) button.disabled = true;
  try {
    await api(path, body);
    show(path === "cancel" ? {state: "cancelled"} : await api("state"));
  } catch (error) {
    try { show(await api("state")); } catch (_) { /* первоначальная ошибка полезнее */ }
    statusNode.textContent = error.message;
  } finally {
    busy = false;
    for (const button of document.querySelectorAll("button")) button.disabled = false;
    showResend();
    if (!terminal.includes(currentState)) timer = setTimeout(refresh, 2000);
  }
}
byId("choose-phone").addEventListener("click", () => { phoneChosen = true; show({state: "idle"}); byId("phone").focus(); });
byId("choose-qr").addEventListener("click", () => submit("qr", {}));
byId("cancel").addEventListener("click", () => submit("cancel", {}));
byId("resend").addEventListener("click", () => {
  if (currentState === "code_required" && resendAfter === 0) submit("resend", {});
});
for (const field of ["phone", "code", "password"]) {
  byId(`${field}-form`).addEventListener("submit", event => {
    event.preventDefault();
    const input = byId(field), value = input.value;
    input.value = "";
    submit(field, {[field]: value});
  });
}
(async () => {
  try {
    let token = new URLSearchParams(location.hash.slice(1)).get("token");
    history.replaceState(null, "", "/connect");
    if (location.protocol !== "https:") {
      token = null;
      throw new Error("Для авторизации откройте HTTPS-ссылку из бота. HTTP доступен только для просмотра страницы.");
    }
    if (token) { await api("exchange", {token}); token = null; }
    await refresh();
  } catch (error) { statusNode.textContent = error.message; }
})();
