/** PSU-2.200：只解释稳定错误类别，不显示原始异常或猜测未确认的原因。 */
export interface SyncFailureExplanation {
  summary: string;
  cause: string;
  advice: string;
  action?: string;
  path?: string;
}

const NETWORK: SyncFailureExplanation = {
  summary: "远程连接失败",
  cause: "未能完成远程请求，可能是连接中断、请求超时或远端暂时不可用。",
  advice: "检查本机网络或代理，再到同步健康页确认远端服务状态。",
  action: "查看同步健康", path: "/sync-health",
};
const KEY: SyncFailureExplanation = {
  summary: "微信密钥不可用",
  cause: "当前微信数据库密钥不可用，无法完成本机导入。",
  advice: "确认微信已登录，到密钥页检查或重新获取密钥。程序不会自动发起授权操作。",
  action: "检查密钥", path: "/start",
};
const AUTH: SyncFailureExplanation = {
  summary: "远程凭据失效",
  cause: "远程登录凭据已失效，当前请求未获授权。",
  advice: "到接入页重新接入这台电脑，处理后可再尝试同步。",
  action: "重新接入", path: "/pairing",
};
const UNKNOWN: SyncFailureExplanation = {
  summary: "同步异常待排查",
  cause: "同步过程中发生异常，现有信息尚不能确定具体原因。",
  advice: "到同步健康页确认服务状态；若反复发生，结合任务时间和诊断代号检查本机日志。",
  action: "查看同步健康", path: "/sync-health",
};
const REASONS: Readonly<Record<string, SyncFailureExplanation>> = {
  REMOTE_UNAVAILABLE: NETWORK,
  NETWORK_UNAVAILABLE: NETWORK,
  REMOTE_RETRYABLE: NETWORK,
  KEY_INVALID: KEY,
  KEY_METHOD_UNAVAILABLE: KEY,
  KEY_ACQUISITION_CANCELLED: KEY,
  REMOTE_AUTH_REQUIRED: AUTH,
  REMOTE_CREDENTIALS_UNAVAILABLE: AUTH,
  REMOTE_PERMISSION_DENIED: {
    summary: "远程权限不足", cause: "远端拒绝了当前设备的访问请求。",
    advice: "检查这台设备是否被撤销或权限是否改变，到接入页重新确认设备接入。",
    action: "检查接入", path: "/pairing",
  },
  REMOTE_ARCHIVE_NOT_FOUND: {
    summary: "远程归档不存在", cause: "当前接入配置指向的远程归档无法找到。",
    advice: "核对远端地址和归档接入配置，确认连接的是正确实例。",
    action: "检查接入", path: "/pairing",
  },
  REMOTE_STORAGE_INSUFFICIENT: {
    summary: "远端可用空间不足", cause: "远端可用存储空间不足，当前上传受到空间检查限制。",
    advice: "查看同步健康页的容量信息，检查远端存储配额和磁盘空间。",
    action: "查看容量", path: "/sync-health",
  },
  OBJECT_STORAGE_NOT_CONFIGURED: {
    summary: "对象存储尚未配置", cause: "远端未配置可用的对象存储，无法接收附件。",
    advice: "到设置页配置对象存储，并验证连接后再尝试。",
    action: "去配置对象存储", path: "/settings#object-storage",
  },
  REMOTE_SYNC_CONFLICT: {
    summary: "上传位置不一致", cause: "远端记的上传位置与本机对不上，自动对齐也没能接上。",
    advice: "检查是否有另一份拷贝以这台电脑的身份同步，关闭重复实例后再尝试；不要直接改上传游标。",
  },
  REMOTE_RECORD_MODEL_TOO_OLD: {
    summary: "远端版本不兼容", cause: "远端记录模型版本无法接收当前同步的数据。",
    advice: "核对本机与远端版本，更新远端服务后再尝试同步。",
  },
  REMOTE_PROTOCOL_ERROR: {
    summary: "远端响应异常", cause: "远端返回的内容不符合当前客户端的接口约定。",
    advice: "核对远端地址、本机与远端版本，以及反向代理是否返回了错误页面。",
    action: "查看同步健康", path: "/sync-health",
  },
  SOURCE_NOT_FOUND: {
    summary: "微信数据源找不到", cause: "本机配置的微信数据源当前无法找到。",
    advice: "确认微信账号已登录，并在开始页检查数据目录是否仍然可用。",
    action: "检查数据源", path: "/start",
  },
  SOURCE_LOCKED: {
    summary: "微信数据源暂不可读", cause: "读取微信数据源时受到占用或访问权限限制。",
    advice: "检查微信状态、数据目录访问权限和本机日志；暂时占用可等待下一轮再试。",
  },
  SNAPSHOT_CLIENT_MUST_CLOSE: {
    summary: "需要关闭微信后读取", cause: "当前快照方式需要微信关闭后才能读取数据源。",
    advice: "先退出微信再尝试导入；重新登录后确认当前读取方式是否适用。",
  },
  SYNC_ATTACHMENT_SOURCE_CHANGED: {
    summary: "需要找回原附件", cause: "现有文件与旧上传记录不同，未找到能通过校验的旧版本。",
    advice: "保留当前文件，尝试从原电脑或备份找回原件；重复同一上传计划不会解决。",
    action: "查看同步健康", path: "/sync-health",
  },
  SYNC_ATTACHMENTS_PENDING: {
    summary: "部分附件未传完", cause: "本轮有附件上传失败，已传完的内容会保留。",
    advice: "查看未完成附件的原因后重试；来源变化或缺失的文件需要单独处理。",
    action: "查看同步健康", path: "/sync-health",
  },
  SYNC_STAGE_STALLED: {
    summary: "同步没有继续推进", cause: "当前阶段仍有待处理内容，但本轮没有取得进展。",
    advice: "查看待处理附件的失败原因、对象存储和远端容量；反复停滞时检查本机日志。",
    action: "查看同步健康", path: "/sync-health",
  },
  SYNC_VERIFICATION_LIMIT: {
    summary: "本轮核验已保存", cause: "本轮核验达到处理上限，仍有待核验内容。",
    advice: "已完成的核验结果已保存，可以继续处理剩余内容；不需要重新扫描数据源。",
    action: "查看同步健康", path: "/sync-health",
  },
  SYNC_STAGE_CRASHED: UNKNOWN,
};

export const explainSyncFailure = (code: string | null): SyncFailureExplanation =>
  code === null ? UNKNOWN : (REASONS[code] ?? UNKNOWN);
