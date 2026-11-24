import { apiError, ChatGPTError, fetchRemote, isObject, jsonResponse } from "./errors.js";
import type { ChatGPTModel } from "./types.js";

export async function listModels(accessToken: string, signal: AbortSignal): Promise<ChatGPTModel[]> {
  const response = await fetchRemote("https://api.openai.com/v1/models", {
    headers: { authorization: `Bearer ${accessToken}`, accept: "application/json" },
    signal,
  });
  const body = await jsonResponse(response);
  if (!response.ok) throw apiError(body, response.status, response.headers.get("x-request-id"));
  if (!isObject(body) || !Array.isArray(body.models)) {
    throw new ChatGPTError("invalid_model_catalog", "ChatGPT returned an unexpected model catalog. Try again.", true, response.status);
  }
  const result: ChatGPTModel[] = [];
  for (const model of body.models) {
    if (!isObject(model) || model.visibility !== "list") continue;
    if (typeof model.slug !== "string" || !model.slug.trim() || model.slug.length > 200 ||
        typeof model.display_name !== "string" || !model.display_name.trim() || model.display_name.length > 200) {
      throw new ChatGPTError("invalid_model_catalog", "ChatGPT returned an incomplete model listing. Try again.", true, response.status);
    }
    const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;
    const rawEfforts = model.supported_reasoning_efforts ?? model.supported_reasoning_levels;
    const efforts = Array.isArray(rawEfforts) ? rawEfforts.flatMap((value) => {
      const effort = typeof value === 'string' ? value : isObject(value) ? value.effort : undefined;
      return typeof effort === 'string' && /^[a-z]{1,16}$/.test(effort) ? [effort] : [];
    }) : [];
    const defaultEffort = model.default_reasoning_effort ?? model.default_reasoning_level;
    result.push({ slug: model.slug, displayName: model.display_name,
      ...(positive(model.context_window) ? { contextWindow: model.context_window } : {}),
      ...(positive(model.max_output_tokens) ? { maxOutputTokens: model.max_output_tokens } : {}),
      ...(efforts.length ? { reasoningEfforts: [...new Set(efforts)] } : {}),
      ...(typeof defaultEffort === 'string' && efforts.includes(defaultEffort) ? { defaultReasoningEffort: defaultEffort } : {}),
    });
  }
  return result;
}
