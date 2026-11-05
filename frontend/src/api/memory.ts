import { apiClient } from './client';
import { MemoryItem, MemoryStats, UserMemorySetting } from '../types';

export interface MemoryListResult {
  items: MemoryItem[];
  total: number;
  page: number;
  page_size: number;
}

export const getMemories = async (params?: {
  memory_type?: string;
  is_active?: boolean;
  search?: string;
  page?: number;
  page_size?: number;
}): Promise<MemoryListResult> => {
  const res = await apiClient.get<MemoryListResult>('/memories', { params });
  return res.data;
};

export const getMemoryStats = async (): Promise<MemoryStats> => {
  const res = await apiClient.get<MemoryStats>('/memories/stats');
  return res.data;
};

export const getMemorySettings = async (): Promise<UserMemorySetting> => {
  const res = await apiClient.get<UserMemorySetting>('/memories/settings');
  return res.data;
};

export const updateMemorySettings = async (data: {
  auto_memory_enabled: boolean;
}): Promise<UserMemorySetting> => {
  const res = await apiClient.put<UserMemorySetting>('/memories/settings', data);
  return res.data;
};

export const createMemory = async (data: {
  content: string;
  memory_type: string;
  tags?: string[];
  confidence?: number;
}): Promise<MemoryItem> => {
  const res = await apiClient.post<MemoryItem>('/memories', data);
  return res.data;
};

export const updateMemory = async (
  id: string,
  data: {
    content?: string;
    memory_type?: string;
    tags?: string[];
    is_active?: boolean;
    confidence?: number;
  }
): Promise<MemoryItem> => {
  const res = await apiClient.put<MemoryItem>(`/memories/${id}`, data);
  return res.data;
};

export const toggleMemoryStatus = async (
  id: string,
  is_active: boolean
): Promise<MemoryItem> => {
  const res = await apiClient.patch<MemoryItem>(`/memories/${id}/status`, null, {
    params: { is_active },
  });
  return res.data;
};

export const deleteMemory = async (id: string): Promise<void> => {
  await apiClient.delete(`/memories/${id}`);
};

export const clearAllMemories = async (): Promise<{ deleted_count: number }> => {
  const res = await apiClient.delete<{ deleted_count: number }>('/memories');
  return res.data;
};
