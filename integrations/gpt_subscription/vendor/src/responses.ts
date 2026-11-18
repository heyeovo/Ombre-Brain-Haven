import OpenAI from "openai";
import { createHash } from "node:crypto";
import { apiError, ChatGPTError, isObject } from "./errors.js";
import type { GptCacheEvidence, GptQuotaSnapshot, GptQuotaWindow, StreamResponseOptions } from "./types.js";

function cacheEvidence(response: unknown): GptCacheEvidence {
  const value = isObject(response) ? response : {};
  const integer = (n: unknown): number | null => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 ? n : null;
  const model = value.model;
  const tier = value.service_tier;
  const usage = isObject(value.usage) ? value.usage : {};
  const details = isObject(usage.input_tokens_details) ? usage.input_tokens_details : {};
  const raw = isObject(value.prompt_cache_diagnostics) ? value.prompt_cache_diagnostics : {};
  const types = ['cache_hit', 'cache_miss', 'comparison_response_not_found', 'unavailable'] as const;
  const reasons = ['model_changed', 'prompt_cache_key_changed', 'service_tier_changed', 'tools_changed',
    'text_format_changed', 'reasoning_effort_changed', 'verbosity_changed', 'context_compacted', 'input_changed'];
  return {
    version: 1,
    response_model: typeof model === 'string' && model.length <= 200 && /^(?:gpt-[A-Za-z0-9._-]+|o[1-9][A-Za-z0-9._-]*)$/.test(model) ? model : null,
    service_tier: typeof tier === 'string' && ['auto', 'default', 'flex', 'scale', 'priority'].includes(tier) ? tier : null,
    cache_write_tokens: integer(details.cache_write_tokens),
    diagnostics: types.includes(raw.type as typeof types[number]) ? {
      type: raw.type as typeof types[number],
      reason: raw.type === 'cache_miss' && typeof raw.reason === 'string' && reasons.includes(raw.reason) ? raw.reason : null,
      comparison_reusable_tokens: integer(raw.comparison_reusable_tokens),
      cache_missed_tokens: integer(raw.cache_missed_tokens),
    } : null,
  };
}

// Haven modification 2026-11-16. Only accept quota fields documented in the
// official Codex parser. Whether the SIWC route emits them needs real verification.
const MAX_QUOTA_BUCKETS = 16;
const quotaId = (value: unknown): string | null => typeof value === "string" && /^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(value)
  ? value.toLowerCase().replaceAll("-", "_") : null;

function quotaNumber(value: unknown, header: boolean): number | null {
  if (header && typeof value === "string") {
    const text = value.trim();
    if (!/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(text)) return null;
    value = Number(text);
  }
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function quotaWindow(percent: unknown, minutes: unknown, reset: unknown, header = false): GptQuotaWindow | null {
  const used = quotaNumber(percent, header);
  if (used === null || used < 0 || used > 100) return null;
  const duration = quotaNumber(minutes, header);
  const epoch = quotaNumber(reset, header);
  return {
    used_percent: used,
    window_minutes: duration !== null && Number.isSafeInteger(duration) && duration > 0 ? duration : null,
    resets_at: epoch !== null && Number.isSafeInteger(epoch) && epoch > 0 && epoch <= 253402300799 ? epoch : null,
  };
}

function quotaHeaders(headers: Headers): GptQuotaSnapshot[] {
  const ids = new Set<string>();
  for (const name of headers.keys()) {
    // Deliberately exclude API RPM/TPM x-ratelimit-* and unrelated headers.
    const match = /^(x-codex(?:-[a-z0-9]+)*)-(?:primary|secondary)-used-percent$/.exec(name);
    if (match?.[1] && match[1].length <= 65 && ids.size < MAX_QUOTA_BUCKETS) ids.add(match[1]);
  }
  const snapshots: GptQuotaSnapshot[] = [];
  const observed_at = new Date().toISOString();
  for (const prefix of ids) {
    const read = (window: string) => quotaWindow(headers.get(`${prefix}-${window}-used-percent`),
      headers.get(`${prefix}-${window}-window-minutes`), headers.get(`${prefix}-${window}-reset-at`), true);
    const primary = read("primary");
    const secondary = read("secondary");
    if (primary || secondary) snapshots.push({ source: "response_headers", observed_at,
      limit_id: prefix.slice(2).replaceAll("-", "_"), scope: "unverified", primary, secondary });
  }
  return snapshots;
}

function quotaEvent(event: Record<string, unknown>): GptQuotaSnapshot | null {
  const id = quotaId(event.metered_limit_name ?? event.limit_name ?? "codex");
  if (!id || !isObject(event.rate_limits)) return null;
  const read = (value: unknown) => isObject(value)
    ? quotaWindow(value.used_percent, value.window_minutes, value.reset_at) : null;
  const primary = read(event.rate_limits.primary);
  const secondary = read(event.rate_limits.secondary);
  return primary || secondary ? { source: "stream_event", observed_at: new Date().toISOString(),
    limit_id: id, scope: "unverified", primary, secondary } : null;
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

export async function streamResponse(
  accessToken: string,
  options: StreamResponseOptions,
  signal: AbortSignal,
): Promise<{ text: string }> {
  const input = typeof options.input === "string" ? [{ role: "user" as const, content: options.input }] : options.input;
  if (!Array.isArray(input) || input.some((message) =>
    !isObject(message) || (!(
      "role" in message && ["user", "assistant", "developer"].includes(String(message.role)) && typeof message.content === "string"
    ) && !(options.tools && "type" in message && ["message", "reasoning", "function_call", "function_call_output"].includes(String(message.type))))
  )) {
    throw new ChatGPTError("invalid_request", "Use text messages with user, assistant, or developer roles. Put system guidance in instructions.");
  }

  // The DevKit supplies the selected ChatGPT account's refreshed OAuth token.
  // Leave org/project unset; they are not placeholders for a Platform API account.
  const client = new OpenAI({
    apiKey: accessToken,
    baseURL: "https://api.openai.com/v1",
    organization: null,
    project: null,
    // Keep the SDK's default without inheriting OPENAI_LOG=debug, which can log clipboard text.
    logLevel: "warn",
    // Report ChatGPT sharing-limit errors immediately instead of retrying HTTP 429s.
    maxRetries: 0,
    // A redirect can forward the clipboard body to another origin.
    fetchOptions: { redirect: "error" },
    // Also prevent OPENAI_CUSTOM_HEADERS from replacing this account's credentials or tenant.
    defaultHeaders: {
      authorization: `Bearer ${accessToken}`,
      "openai-organization": null,
      "openai-project": null,
    },
  });
  // The SDK's timeout ends at headers; keep the existing deadline on the body too.
  const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(180_000)]);
  let response: Response | undefined;
  try {
    const body = {
      model: options.model,
      input: input.map(message => "type" in message && message.type ? message
        : "role" in message && typeof message.content === "string" ? { role: message.role, content: message.content } : message),
      ...(options.instructions !== undefined ? { instructions: options.instructions } : {}),
      ...(options.tools ? { tools: options.tools } : {}),
      ...(options.tool_choice ? { tool_choice: options.tool_choice } : {}),
      ...(options.include ? { include: options.include } : {}),
      store: false as const,
      stream: true as const,
    };
    if (options.expectedRequestHash) {
      const body_hash = createHash('sha256').update(canonical(body)).digest('hex');
      if (body_hash !== options.expectedRequestHash || !options.onRequest) throw new ChatGPTError('audit_mismatch', 'Request audit did not match. No request was sent.');
      await options.onRequest({ body_hash });
      requestSignal.throwIfAborted();
    }
    const result = await client.responses.create(body, { signal: requestSignal, headers: { accept: "text/event-stream" } }).withResponse();
    response = result.response;
    options.onReceipt?.({ ...(result.request_id && /^[a-zA-Z0-9_-]{1,160}$/.test(result.request_id) ? { requestId: result.request_id } : {}) });
    const headerQuotas = quotaHeaders(response.headers);
    const eventQuotas = new Map<string, GptQuotaSnapshot>();
    const contentType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
    // The direct route can return valid SSE without Content-Type. In that case,
    // validate the events below and still require response.completed for success.
    if (!response.body || (contentType && contentType !== "text/event-stream")) {
      await response.body?.cancel();
      throw new ChatGPTError("invalid_stream", "ChatGPT did not return the expected response stream.", true, response.status);
    }

    let completed = false;
    let text = "";
    for await (const streamed of result.data) {
      requestSignal.throwIfAborted();
      // The public SDK union does not declare Codex's optional extension event.
      const event: unknown = streamed;
      if (!isObject(event)) continue;
      if (event.type === "codex.rate_limits") {
        const snapshot = quotaEvent(event);
        // Latest event per bucket, without combining different observations.
        if (snapshot && (eventQuotas.has(snapshot.limit_id) || eventQuotas.size < MAX_QUOTA_BUCKETS)) {
          eventQuotas.set(snapshot.limit_id, snapshot);
        }
      } else if (event.type === "response.output_text.delta" && typeof event.delta === "string") {
        text += event.delta;
        if (text.length > 16 * 1024 * 1024) throw new ChatGPTError("response_too_large", "The response was too large. Try a smaller request.");
        options.onDelta?.(event.delta);
      } else if (event.type === "response.failed" || event.type === "error") {
        const detail = event.type === "response.failed" ? event.response : event;
        throw apiError(detail, response.status, result.request_id);
      } else if (event.type === "response.incomplete") {
        throw new ChatGPTError("response_incomplete", "ChatGPT stopped before completing the response. You can keep the partial text or try again.", true);
      } else if (event.type === "response.completed") {
        const output = isObject(event.response) ? event.response.output : undefined;
        if (Array.isArray(output) && output.some(item => isObject(item) && item.type === "function_call")) {
          if (!options.onToolContinuation || output.length > 32 || Buffer.byteLength(JSON.stringify(output)) > 128 * 1024) {
            throw new ChatGPTError("tool_output_invalid", "GPT tool call batch is unsupported or too large.");
          }
          options.onToolContinuation(output);
        }
        // Haven modification 2026-11-15: expose allowlisted actual usage; absent is unknown.
        const raw = isObject(event.response) && isObject(event.response.usage) ? event.response.usage : undefined;
        const usage: Record<string, number> = {};
        for (const key of ["input_tokens", "output_tokens", "total_tokens"]) {
          const value = raw?.[key];
          if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) usage[key] = value;
        }
        for (const [group, key] of [["input_tokens_details", "cached_tokens"], ["output_tokens_details", "reasoning_tokens"]] as const) {
          const detail = raw?.[group];
          const value = isObject(detail) ? detail[key] : undefined;
          if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) usage[key] = value;
        }
        const id = isObject(event.response) ? event.response.id : undefined;
        const snapshots = [...headerQuotas, ...eventQuotas.values()];
        options.onCompleted?.({
          ...(typeof id === "string" && /^[a-zA-Z0-9_-]{1,200}$/.test(id) ? { responseId: id } : {}),
          ...(result.request_id && /^[a-zA-Z0-9_-]{1,160}$/.test(result.request_id) ? { requestId: result.request_id } : {}),
          usage: Object.keys(usage).length ? usage : null,
          quotaEvidence: { status: snapshots.length ? "observed" : "unavailable", snapshots },
          cacheEvidence: cacheEvidence(event.response),
        });
        completed = true;
        break;
      }
    }
    // The SDK may finish iteration on an aborted connection without throwing.
    requestSignal.throwIfAborted();
    if (!completed) throw new ChatGPTError("stream_interrupted", "The response ended before completion. You can keep the partial text or try again.", true);
    return { text };
  } catch (error) {
    if (signal.aborted) throw new ChatGPTError("cancelled", "The response was cancelled.");
    if (error instanceof ChatGPTError) throw error;
    // SDK errors can contain submitted text. Only expose our sanitized diagnostics,
    // including errors the SDK raises while decoding an SSE 'error' event.
    if (error instanceof OpenAI.APIError && !(error instanceof OpenAI.APIConnectionError) && !(error instanceof OpenAI.APIUserAbortError)) {
      // The SDK also unwraps the string in an OAuth { error: "code" } response.
      const detail = typeof error.error === "string" ? { error: error.error } : error.error;
      throw apiError(detail, error.status ?? response?.status, error.requestID ?? response?.headers.get("x-request-id"));
    }
    if (!response) throw new ChatGPTError("network_error", "Could not reach ChatGPT. Check your connection and try again.", true);
    if (error instanceof SyntaxError) throw new ChatGPTError("invalid_stream", "The response stream contained an invalid event. Try again.", true);
    throw new ChatGPTError("stream_interrupted", "The connection was interrupted. You can keep the partial text or try again.", true);
  }
}
