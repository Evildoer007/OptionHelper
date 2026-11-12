const secretKeys = new Set(["password", "token", "api_key", "secret", "secret_value", "private_key"]);

export async function request(path, options = {}) {
  const { headers = {}, ...requestOptions } = options;
  const response = await fetch(path, {
    ...requestOptions,
    headers: { "Content-Type": "application/json", ...headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.message || body.detail || body.reason || body.next_step || body.error?.next_step || body.error || "请求未完成");
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

export function safeJson(value, { allowSecrets = new Set() } = {}) {
  const visit = (item) => {
    if (Array.isArray(item)) return item.map(visit);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.entries(item).map(([key, child]) => {
        if (secretKeys.has(key) && !allowSecrets.has(key)) throw new Error("明文凭据只能通过设置中心的专用凭据表单保存。");
        return [key, visit(child)];
      }));
    }
    return item;
  };
  return JSON.stringify(visit(value));
}
