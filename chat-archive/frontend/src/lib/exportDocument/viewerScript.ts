/**
 * 压缩包里那份网页自带的一小段脚本与样式（PSU-2.179，用户在原型第 3 块选了 A）。
 *
 * 做四件事，全部只读网页旁边的相对路径，**不发任何网络请求、不引第三方**：
 *
 * 1. 点图片：页面里放大，左右翻（←/→）、滚轮缩放、Esc 关；另给「打开原文件」。
 * 2. 点语音：App 里那种气泡，点一下播、再点停；同一时刻只放一条。
 * 3. 点视频：浮层里一个 `<video controls>`。
 * 4. 探针：`附件/.here.png` 加载不到＝附件目录不在旁边（多半是没解压就在压缩包里
 *    双击打开的），这时才亮出黄条。正常解压后看不到它。
 *
 * 文件卡是普通的 `<a href>`，交给浏览器与系统，不需要脚本。
 * 有的邮箱预览、安全软件会拦脚本：那时文字与图片照样能看，只是点图不会放大。
 */

export const VIEWER_STYLE = `
.bundle-unzipped{display:none;margin:0 0 12px;padding:8px 12px;border-radius:8px;font-size:13px;
  background:#faf3e3;border:1px solid #e3d0a0;color:#7a5a1e}
.bundle-unzipped.is-shown{display:block}
img[data-bundle-src]{cursor:zoom-in}
.bundle-voice{display:inline-flex;align-items:center;gap:10px;min-width:150px;padding:8px 12px;
  border-radius:8px;border:1px solid #efe8d8;background:#fbf7ef;cursor:pointer;user-select:none;font-size:13px}
.bundle-voice .bundle-voice-icon{width:24px;height:24px;border-radius:50%;background:rgba(0,0,0,.07);
  display:inline-grid;place-items:center;font-size:11px}
.bundle-voice .bundle-voice-wave{flex:1;height:12px;opacity:.45;
  background:repeating-linear-gradient(90deg,#5b5346 0 2px,transparent 2px 6px)}
.bundle-voice.is-playing .bundle-voice-wave{opacity:.9;
  background:repeating-linear-gradient(90deg,#137a3d 0 2px,transparent 2px 6px)}
.bundle-video{position:relative;display:grid;place-items:center;width:220px;height:140px;border-radius:8px;
  background:linear-gradient(160deg,#6d6a63,#3d3a34);color:#fff;cursor:pointer;font-size:26px}
.bundle-video .bundle-video-duration{position:absolute;right:6px;bottom:5px;font-size:11px;
  background:rgba(0,0,0,.45);border-radius:4px;padding:0 4px}
.bundle-file{text-decoration:none;color:inherit;display:block}
.bundle-missing{display:inline-block;padding:8px 11px;border:1px dashed #ddd3bf;border-radius:8px;
  font-size:12.5px;color:#6e6760;background:#fdfaf3}
.bundle-overlay{position:fixed;inset:0;z-index:50;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:12px;background:rgba(20,18,15,.88);color:#eee;font:13px system-ui,sans-serif}
.bundle-overlay img,.bundle-overlay video{max-width:90vw;max-height:78vh;transition:transform .08s}
.bundle-overlay-bar{display:flex;gap:16px;align-items:center}
.bundle-overlay-bar button,.bundle-overlay-bar a{color:#eee;background:none;border:1px solid #777;
  border-radius:14px;padding:3px 12px;font:inherit;cursor:pointer;text-decoration:none}
.bundle-overlay-close{position:absolute;top:12px;right:16px;font-size:20px;background:none;border:0;
  color:#eee;cursor:pointer}
`;

export const VIEWER_SCRIPT = `(function () {
  "use strict";
  var doc = document;
  var banner = doc.querySelector(".bundle-unzipped");
  var probe = doc.body.getAttribute("data-bundle-probe");
  if (banner && probe) {
    var check = new Image();
    check.onerror = function () { banner.classList.add("is-shown"); };
    check.src = probe;
  }

  var overlay = null;
  var keyHandler = null;
  function close() {
    if (overlay) { overlay.remove(); overlay = null; }
    if (keyHandler) { doc.removeEventListener("keydown", keyHandler); keyHandler = null; }
  }
  function open(build) {
    close();
    overlay = doc.createElement("div");
    overlay.className = "bundle-overlay";
    overlay.setAttribute("role", "dialog");
    var shut = doc.createElement("button");
    shut.className = "bundle-overlay-close";
    shut.setAttribute("aria-label", "关闭");
    shut.textContent = "✕";
    shut.onclick = close;
    overlay.appendChild(shut);
    overlay.addEventListener("click", function (event) { if (event.target === overlay) close(); });
    build(overlay);
    doc.body.appendChild(overlay);
  }

  var images = Array.prototype.slice.call(doc.querySelectorAll("img[data-bundle-src]"));
  function showImage(index) {
    open(function (layer) {
      var scale = 1;
      var big = doc.createElement("img");
      var bar = doc.createElement("div");
      bar.className = "bundle-overlay-bar";
      var prev = doc.createElement("button");
      prev.textContent = "‹ 上一张";
      var count = doc.createElement("span");
      var next = doc.createElement("button");
      next.textContent = "下一张 ›";
      var file = doc.createElement("a");
      file.textContent = "打开原文件";
      file.target = "_blank";
      function go(to) {
        index = (to + images.length) % images.length;
        scale = 1;
        big.style.transform = "";
        big.src = images[index].getAttribute("data-bundle-src");
        file.href = big.src;
        count.textContent = (index + 1) + " / " + images.length;
      }
      prev.onclick = function () { go(index - 1); };
      next.onclick = function () { go(index + 1); };
      big.addEventListener("wheel", function (event) {
        event.preventDefault();
        scale = Math.min(6, Math.max(1, scale * (event.deltaY < 0 ? 1.15 : 1 / 1.15)));
        big.style.transform = "scale(" + scale + ")";
      });
      if (images.length > 1) { bar.appendChild(prev); bar.appendChild(count); bar.appendChild(next); }
      bar.appendChild(file);
      layer.appendChild(big);
      layer.appendChild(bar);
      keyHandler = function (event) {
        if (event.key === "Escape") close();
        else if (event.key === "ArrowLeft") go(index - 1);
        else if (event.key === "ArrowRight") go(index + 1);
      };
      doc.addEventListener("keydown", keyHandler);
      go(index);
    });
  }
  images.forEach(function (image, index) {
    image.addEventListener("click", function () { showImage(index); });
  });

  var playing = null;
  var player = null;
  function stopVoice() {
    if (player) { player.pause(); player = null; }
    if (playing) { playing.classList.remove("is-playing"); playing = null; }
  }
  Array.prototype.forEach.call(doc.querySelectorAll("[data-bundle-audio]"), function (voice) {
    voice.addEventListener("click", function () {
      var again = playing === voice;
      stopVoice();
      if (again) return;
      playing = voice;
      voice.classList.add("is-playing");
      player = new Audio(voice.getAttribute("data-bundle-audio"));
      player.onended = stopVoice;
      player.onerror = stopVoice;
      var started = player.play();
      if (started && typeof started.catch === "function") started.catch(stopVoice);
    });
  });

  Array.prototype.forEach.call(doc.querySelectorAll("[data-bundle-video]"), function (tile) {
    tile.addEventListener("click", function () {
      stopVoice();
      open(function (layer) {
        var video = doc.createElement("video");
        video.controls = true;
        video.autoplay = true;
        video.src = tile.getAttribute("data-bundle-video");
        layer.appendChild(video);
        keyHandler = function (event) { if (event.key === "Escape") close(); };
        doc.addEventListener("keydown", keyHandler);
      });
    });
  });
})();`;
