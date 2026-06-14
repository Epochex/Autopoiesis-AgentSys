const SECRET_KEY_PATTERN = /(api[_-]?key|authorization|bearer|token|password|passwd|secret|credential|private[_-]?key)/i;

export interface RedactionOptions {
  replacement?: string;
  maxStringLength?: number;
}

export function redactForTrace(value: unknown, options: RedactionOptions = {}): unknown {
  return redactValue(value, {
    replacement: options.replacement ?? "[REDACTED]",
    maxStringLength: options.maxStringLength ?? 4000,
  });
}

function redactValue(value: unknown, options: Required<RedactionOptions>): unknown {
  if (typeof value === "string") {
    return value.length > options.maxStringLength ? `${value.slice(0, options.maxStringLength)}[TRUNCATED]` : value;
  }
  if (!value || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => redactValue(item, options));
  const output: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    output[key] = SECRET_KEY_PATTERN.test(key) ? options.replacement : redactValue(item, options);
  }
  return output;
}
