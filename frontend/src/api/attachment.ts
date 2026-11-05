import { apiClient } from './client';
import { ConversationAttachment, ConversationAttachmentDetail } from '../types';

/**
 * Upload one or more documents (PDF, DOCX, TXT, MD) as conversation attachments.
 */
export const uploadAttachments = async (
  conversationId: string,
  files: File[]
): Promise<ConversationAttachment[]> => {
  const formData = new FormData();
  for (const file of files) {
    formData.append('files', file);
  }

  const res = await apiClient.post<ConversationAttachment[]>(
    `/conversations/${conversationId}/attachments`,
    formData,
    {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    }
  );
  return res.data;
};

/**
 * List all attachments belonging to a conversation.
 */
export const getAttachments = async (
  conversationId: string
): Promise<ConversationAttachment[]> => {
  const res = await apiClient.get<ConversationAttachment[]>(
    `/conversations/${conversationId}/attachments`
  );
  return res.data;
};

/**
 * Get detailed metadata and extracted parsed content for an attachment.
 */
export const getAttachmentDetail = async (
  conversationId: string,
  attachmentId: string
): Promise<ConversationAttachmentDetail> => {
  const res = await apiClient.get<ConversationAttachmentDetail>(
    `/conversations/${conversationId}/attachments/${attachmentId}`
  );
  return res.data;
};

/**
 * Delete a conversation attachment and remove physical disk files.
 */
export const deleteAttachment = async (
  conversationId: string,
  attachmentId: string
): Promise<void> => {
  await apiClient.delete(
    `/conversations/${conversationId}/attachments/${attachmentId}`
  );
};
