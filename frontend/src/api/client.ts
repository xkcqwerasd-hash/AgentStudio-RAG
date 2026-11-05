import axios from 'axios';

// Tab-scoped storage key for refresh persistence (per instruction: strictly no permanent localStorage)
const AUTH_TOKEN_KEY = 'agentstudio_session_token';

// In-memory token reference for quick access
let inMemoryToken: string | null = null;

// Initialize token from sessionStorage if present in the current browser tab
try {
  inMemoryToken = sessionStorage.getItem(AUTH_TOKEN_KEY);
} catch {
  // Ignore environments without sessionStorage
}

// Callback registered by AuthContext for handling 401 Unauthorized responses
let unauthorizedHandler: (() => void) | null = null;

export const setUnauthorizedHandler = (handler: (() => void) | null) => {
  unauthorizedHandler = handler;
};

export const getAuthToken = (): string | null => {
  if (inMemoryToken) {
    return inMemoryToken;
  }
  try {
    const stored = sessionStorage.getItem(AUTH_TOKEN_KEY);
    if (stored) {
      inMemoryToken = stored;
      return stored;
    }
  } catch {
    // Ignore
  }
  return null;
};

export const setAuthToken = (token: string | null) => {
  inMemoryToken = token;
  try {
    if (token) {
      sessionStorage.setItem(AUTH_TOKEN_KEY, token);
    } else {
      sessionStorage.removeItem(AUTH_TOKEN_KEY);
    }
  } catch {
    // Ignore
  }
};

export const clearAuthToken = () => {
  setAuthToken(null);
};

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor: dynamically inject Bearer token
apiClient.interceptors.request.use((config) => {
  const token = getAuthToken();
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Response interceptor: handle 401s and extract error messages
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      clearAuthToken();
      if (unauthorizedHandler) {
        unauthorizedHandler();
      }
    }
    const detail = error.response?.data?.detail || error.message || '网络请求错误';
    return Promise.reject(new Error(detail));
  }
);
