import { apiClient } from './client';
import {
  AuthModeInfo,
  LoginCredentials,
  LogoutResponse,
  PasswordChangeData,
  RegisterData,
  TokenResponse,
  UserProfile,
} from '../types';

/**
 * Fetch system deployment mode and public registration capability.
 * Public endpoint, does not require authentication.
 */
export const getAuthMode = async (): Promise<AuthModeInfo> => {
  const response = await apiClient.get<AuthModeInfo>('/auth/mode');
  return response.data;
};

/**
 * Authenticate user credentials and retrieve JWT bearer token.
 */
export const loginUser = async (credentials: LoginCredentials): Promise<TokenResponse> => {
  const response = await apiClient.post<TokenResponse>('/auth/login', credentials);
  return response.data;
};

/**
 * Register a new user account.
 */
export const registerUser = async (data: RegisterData): Promise<UserProfile> => {
  const response = await apiClient.post<UserProfile>('/auth/register', data);
  return response.data;
};

/**
 * Get currently authenticated user profile.
 */
export const getCurrentUser = async (): Promise<UserProfile> => {
  const response = await apiClient.get<UserProfile>('/auth/me');
  return response.data;
};

/**
 * Logout current user session.
 * Notes: Server transparently reports client_discard semantics.
 */
export const logoutUser = async (): Promise<LogoutResponse> => {
  const response = await apiClient.post<LogoutResponse>('/auth/logout');
  return response.data;
};

/**
 * Change user password.
 * Forces token_version bump on backend, revoking all existing sessions.
 */
export const changePasswordUser = async (
  data: PasswordChangeData
): Promise<{ message: string }> => {
  const response = await apiClient.post<{ message: string }>('/auth/password', data);
  return response.data;
};
