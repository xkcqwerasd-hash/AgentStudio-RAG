import { apiClient } from './client';
import {
  ActiveModelItem,
  ModelCapabilities,
  ModelConfig,
  ModelProvider,
  ModelTestResponse,
  OllamaProbeResponse,
} from '../types';

export const getProviders = async (): Promise<ModelProvider[]> => {
  const res = await apiClient.get<ModelProvider[]>('/models/providers');
  return res.data;
};

export const createProvider = async (data: {
  name: string;
  provider_type: string;
  base_url: string;
  api_key?: string;
  enabled?: boolean;
  models?: Array<{
    model_name: string;
    display_name: string;
    model_type?: string;
    is_default?: boolean;
    capabilities?: ModelCapabilities;
  }>;
}): Promise<ModelProvider> => {
  const res = await apiClient.post<ModelProvider>('/models/providers', data);
  return res.data;
};

export const updateProvider = async (
  providerId: string,
  data: {
    name?: string;
    provider_type?: string;
    base_url?: string;
    api_key?: string | null;
    enabled?: boolean;
  }
): Promise<ModelProvider> => {
  const res = await apiClient.put<ModelProvider>(`/models/providers/${providerId}`, data);
  return res.data;
};

export const deleteProvider = async (providerId: string): Promise<void> => {
  await apiClient.delete(`/models/providers/${providerId}`);
};

export const createModelConfig = async (
  providerId: string,
  data: {
    model_name: string;
    display_name: string;
    model_type?: string;
    is_default?: boolean;
    enabled?: boolean;
    capabilities?: ModelCapabilities;
  }
): Promise<ModelConfig> => {
  const res = await apiClient.post<ModelConfig>(`/models/providers/${providerId}/models`, data);
  return res.data;
};

export const updateModelConfig = async (
  modelId: string,
  data: {
    display_name?: string;
    model_type?: string;
    is_default?: boolean;
    enabled?: boolean;
    capabilities?: ModelCapabilities;
  }
): Promise<ModelConfig> => {
  const res = await apiClient.put<ModelConfig>(`/models/configs/${modelId}`, data);
  return res.data;
};

export const deleteModelConfig = async (modelId: string): Promise<void> => {
  await apiClient.delete(`/models/configs/${modelId}`);
};

export const setDefaultModel = async (modelId: string): Promise<ModelConfig> => {
  const res = await apiClient.post<ModelConfig>(`/models/configs/${modelId}/set-default`);
  return res.data;
};

export const testModelConnection = async (data: {
  provider_id?: string;
  model_id?: string;
  provider_type?: string;
  base_url?: string;
  api_key?: string;
  model_name?: string;
}): Promise<ModelTestResponse> => {
  const res = await apiClient.post<ModelTestResponse>('/models/test', data);
  return res.data;
};

export const probeOllama = async (baseUrl: string = 'http://localhost:11434'): Promise<OllamaProbeResponse> => {
  const res = await apiClient.post<OllamaProbeResponse>('/models/probe/ollama', { base_url: baseUrl });
  return res.data;
};

export const probeVLLM = async (
  baseUrl: string = 'http://localhost:8000/v1',
  apiKey?: string
): Promise<{ success: boolean; models: string[]; error?: string }> => {
  const res = await apiClient.post('/models/probe/vllm', { base_url: baseUrl, api_key: apiKey });
  return res.data;
};

export const getActiveModels = async (): Promise<ActiveModelItem[]> => {
  const res = await apiClient.get<ActiveModelItem[]>('/models/active');
  return res.data;
};
