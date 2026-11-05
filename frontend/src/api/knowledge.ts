import { apiClient } from './client';
import { DocumentChunk, DocumentItem, KnowledgeBase } from '../types';

export const getKnowledgeBases = async (): Promise<KnowledgeBase[]> => {
  const res = await apiClient.get<KnowledgeBase[]>('/knowledge-bases');
  return res.data;
};

export const createKnowledgeBase = async (data: {
  name: string;
  description?: string;
  chunk_size?: number;
  chunk_overlap?: number;
}): Promise<KnowledgeBase> => {
  const res = await apiClient.post<KnowledgeBase>('/knowledge-bases', data);
  return res.data;
};

export const deleteKnowledgeBase = async (id: string): Promise<void> => {
  await apiClient.delete(`/knowledge-bases/${id}`);
};

export const getDocuments = async (kbId: string): Promise<DocumentItem[]> => {
  const res = await apiClient.get<DocumentItem[]>(`/knowledge-bases/${kbId}/documents`);
  return res.data;
};

export const UPLOAD_LIMITS = {
  MAX_FILE_SIZE_MB: 20,
  MAX_FILE_SIZE_BYTES: 20 * 1024 * 1024,
  ALLOWED_EXTENSIONS: ['.pdf', '.md', '.markdown', '.txt'] as const,
};

export const uploadDocument = async (kbId: string, file: File): Promise<DocumentItem> => {
  if (!file || !(file instanceof File)) {
    throw new Error('无效的文件对象，无法上传');
  }
  if (file.size > UPLOAD_LIMITS.MAX_FILE_SIZE_BYTES) {
    throw new Error(`文件 "${file.name}" 超过 ${UPLOAD_LIMITS.MAX_FILE_SIZE_MB}MB 大小限制`);
  }
  const formData = new FormData();
  formData.append('file', file);
  const res = await apiClient.post<DocumentItem>(
    `/knowledge-bases/${kbId}/documents/upload`,
    formData,
    {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    }
  );
  return res.data;
};

export const getDocumentChunks = async (
  docId: string,
  page: number = 1,
  pageSize: number = 20
): Promise<{ items: DocumentChunk[]; total: number }> => {
  const res = await apiClient.get<{ items: DocumentChunk[]; total: number }>(
    `/documents/${docId}/chunks`,
    { params: { page, page_size: pageSize } }
  );
  return res.data;
};

export const deleteDocument = async (docId: string): Promise<void> => {
  await apiClient.delete(`/documents/${docId}`);
};
