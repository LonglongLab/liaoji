<script setup lang="ts">
import longlongMark from "@/assets/brand/longlong-mark.svg";
import SettingsTip from "@/components/SettingsTip.vue";
import { useWorkspace } from "@/lib/workspaceContext";
import {
  appVersion,
  chatlogKeeper,
  chatlogKeeperLicense,
  contractVersion,
  frontendDependencies as dependencies,
  referenceProjects,
} from "@/lib/buildInfo";

const workspace = useWorkspace();
</script>

<template>
  <section
    id="about"
    class="settings-group"
    aria-label="关于"
  >
    <h3 class="settings-group-title">
      关于
    </h3>
    <div class="settings-list">
      <div
        class="settings-row"
        data-testid="about-version"
      >
        <div class="settings-row-main">
          <div class="settings-row-title">
            应用版本
          </div>
          <div class="settings-row-sub">
            聊记{{ workspace === 'remote' ? '远程' : '本地' }} API {{ contractVersion }}
          </div>
        </div>
        <div class="settings-row-trail">
          <span class="settings-muted">{{ appVersion }}</span>
        </div>
      </div>
      <div
        class="settings-row"
        data-testid="about-parser"
      >
        <div class="settings-row-main">
          <div class="settings-row-title">
            微信解析组件
            <SettingsTip label="解析组件的来源">
              上游 commit
              <span class="about-mono">{{ chatlogKeeper.commit.slice(0, 12) }}</span>
              · 取回于 {{ chatlogKeeper.retrieved_on }}
            </SettingsTip>
          </div>
          <div class="settings-row-sub">
            最新版微信解析能力由 chatlog-keeper 提供（{{ chatlogKeeper.license }} License）。
            本应用保留其许可证与版权信息。
            <template v-if="workspace === 'remote'">
              微信解析组件仅在本地端运行。
            </template>
          </div>
        </div>
        <div class="settings-row-trail">
          <span class="settings-muted">{{ chatlogKeeper.name }} {{ chatlogKeeper.version }}</span>
        </div>
      </div>
      <details class="about-details">
        <summary class="settings-row">
          <span class="settings-row-main settings-row-title">
            chatlog-keeper MIT License 全文
          </span>
          <span
            class="settings-row-chevron"
            aria-hidden="true"
          >›</span>
        </summary>
        <div class="settings-row-extra">
          <pre class="about-license-text">{{ chatlogKeeperLicense }}</pre>
        </div>
      </details>
      <details class="about-details">
        <summary class="settings-row">
          <span class="settings-row-main settings-row-title">
            前端第三方依赖（{{ dependencies.length }}）
          </span>
          <span
            class="settings-row-chevron"
            aria-hidden="true"
          >›</span>
        </summary>
        <div class="settings-row-extra">
          <ul class="about-list">
            <li
              v-for="dependency in dependencies"
              :key="dependency.name"
            >
              {{ dependency.name }} <span class="settings-muted">{{ dependency.version }}</span>
            </li>
          </ul>
        </div>
      </details>
      <details class="about-details">
        <summary class="settings-row">
          <span class="settings-row-main settings-row-title">
            参考项目与答谢（{{ referenceProjects.length }}）
          </span>
          <span
            class="settings-row-chevron"
            aria-hidden="true"
          >›</span>
        </summary>
        <div class="settings-row-extra">
          <ul class="about-list">
            <li
              v-for="project in referenceProjects"
              :key="project.name"
            >
              <a
                v-if="project.upstream_url"
                :href="project.upstream_url"
                target="_blank"
                rel="noreferrer"
              >{{ project.name }}</a>
              <span v-else>{{ project.name }}</span>
              <span class="settings-muted">
                · {{ project.role }} · {{ project.license }}
                <template v-if="project.version"> · {{ project.version }}</template>
                <span
                  v-if="project.commit"
                  class="about-mono"
                >
                  · {{ project.commit.slice(0, 12) }}
                </span>
              </span>
            </li>
          </ul>
          <p class="settings-muted about-note">
            这些项目只用于学习、对比和验证实现。
            <template v-if="workspace === 'remote'">
              远程端不运行微信解析组件。
            </template>
            <template v-else>
              除上面的解析组件外，都不随本应用运行。
            </template>
            CipherTalk 的使用遵循其 CC BY-NC-SA 4.0 许可证。
          </p>
        </div>
      </details>
    </div>
    <section
      class="longlong-credit"
      aria-label="Longlong 开发者署名"
    >
      <img
        :src="longlongMark"
        width="80"
        height="80"
        alt=""
      >
      <strong class="longlong-credit-name">Longlong</strong>
      <span>由 <b>Longlong</b> 构建</span>
      <span>AI 辅助开发</span>
      <nav aria-label="Longlong 的主页">
        <a
          href="https://github.com/LonglongLab"
          target="_blank"
          rel="noopener noreferrer"
        >GitHub ↗</a>
        <a
          href="https://x.com/LonglongWithAI"
          target="_blank"
          rel="noopener noreferrer"
        >X ↗</a>
      </nav>
    </section>
  </section>
</template>

<style scoped>
.longlong-credit {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.375rem;
  max-width: 28rem;
  min-width: 0;
  margin: 0 auto;
  padding: 2rem 1.25rem;
  color: var(--ink-soft);
  font-size: 0.8125rem;
  line-height: 1.6;
  text-align: center;
  overflow-wrap: anywhere;
}

.longlong-credit img {
  display: block;
  width: 5rem;
  height: 5rem;
  margin-bottom: 0.5rem;
}

.longlong-credit-name {
  margin-bottom: 0.375rem;
  color: var(--ink);
  font-size: 1.75rem;
  font-weight: 750;
  line-height: 1.2;
  letter-spacing: -0.04em;
}

.longlong-credit b {
  font-weight: 600;
}

.longlong-credit nav {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.5rem;
  max-width: 100%;
}

.longlong-credit a {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 2.75rem;
  min-height: 2.75rem;
  padding: 0.375rem 0.75rem;
  border-radius: 0.375rem;
  color: inherit;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.longlong-credit a:hover {
  color: var(--ink);
}

.longlong-credit a:focus-visible {
  outline: 3px solid var(--accent-ink);
  outline-offset: 3px;
}

.about-details > summary {
  min-height: 52px;
  cursor: pointer;
  list-style: none;
}

.about-details > summary::-webkit-details-marker {
  display: none;
}

.about-details > summary:hover {
  background: var(--surface-sunken);
}

.about-details .settings-row-chevron {
  transition: transform 0.15s ease;
}

.about-details[open] .settings-row-chevron {
  transform: rotate(90deg);
}

.about-license-text {
  max-height: 20rem;
  margin: 0;
  overflow: auto;
  padding: 0.75rem;
  border-radius: 0.5rem;
  background: var(--surface-sunken);
  color: var(--ink-soft);
  font-size: 0.75rem;
  white-space: pre-wrap;
}

.about-list {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  margin: 0;
  padding: 0;
  color: var(--ink);
  font-size: 0.8125rem;
  list-style: none;
}

.about-list a {
  color: var(--link);
}

.about-note {
  margin: 0.75rem 0 0;
}

.about-mono {
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
}
</style>
