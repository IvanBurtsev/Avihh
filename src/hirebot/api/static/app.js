/*
 * Минимальное улучшение поверх серверного рендеринга.
 *
 * Все действия (прогнать шаг, отправить сообщение, забронировать слот) работают
 * обычными HTML-формами — интерфейс полностью работоспособен с отключённым JS.
 * Этот файл добавляет только одно удобство: переписка сама подтягивает новые
 * сообщения, пока вкладка открыта.
 */
(function () {
  "use strict";

  var REFRESH_MS = 5000;

  var container = document.getElementById("messages");
  if (!container) {
    return;
  }
  var conversationId = container.getAttribute("data-conversation-id");
  if (!conversationId) {
    var match = window.location.pathname.match(/\/conversations\/([0-9a-f-]{36})/i);
    if (!match) {
      return;
    }
    conversationId = match[1];
  }

  var timer = null;
  var failures = 0;

  function replaceMessages(html) {
    var parser = new DOMParser();
    var doc = parser.parseFromString(html, "text/html");
    var incoming = doc.getElementById("messages");
    if (!incoming) {
      return;
    }
    // Сравниваем разметку, чтобы не трогать DOM и не сбрасывать прокрутку зря.
    if (incoming.innerHTML === container.innerHTML) {
      return;
    }
    var atBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 24;
    container.innerHTML = incoming.innerHTML;
    if (atBottom) {
      container.scrollTop = container.scrollHeight;
    }
  }

  function refresh() {
    if (document.hidden) {
      schedule();
      return;
    }
    fetch("/ui/conversations/" + conversationId + "/messages-fragment", {
      headers: { Accept: "text/html" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.text();
      })
      .then(function (html) {
        failures = 0;
        replaceMessages(html);
      })
      .catch(function () {
        // Не зацикливаемся на ошибках: сеть недоступна или сессия истекла.
        failures += 1;
        if (failures > 5) {
          return;
        }
      })
      .then(schedule);
  }

  function schedule() {
    if (timer !== null) {
      clearTimeout(timer);
    }
    timer = setTimeout(refresh, REFRESH_MS);
  }

  // Прокрутка к последнему сообщению при загрузке страницы.
  container.scrollTop = container.scrollHeight;
  schedule();
})();
