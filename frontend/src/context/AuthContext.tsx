import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import {
  AuthModeInfo,
  LoginCredentials,
  PasswordChangeData,
  RegisterData,
  UserProfile,
} from '../types';
import {
  getAuthMode,
  getCurrentUser,
  loginUser,
  logoutUser,
  registerUser,
  changePasswordUser,
} from '../api/auth';
import {
  clearAuthToken,
  getAuthToken,
  setAuthToken,
  setUnauthorizedHandler,
} from '../api/client';

export interface AuthContextType {
  currentUser: UserProfile | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  deploymentMode: 'local' | 'server';
  allowRegistration: boolean;
  systemVersion: string;
  login: (credentials: LoginCredentials) => Promise<void>;
  register: (data: RegisterData) => Promise<UserProfile>;
  logout: () => Promise<void>;
  refreshCurrentUser: () => Promise<void>;
  changePassword: (data: PasswordChangeData) => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

// Local mode default user profile representation
const LOCAL_DEFAULT_PROFILE: UserProfile = {
  id: 'usr_local_default',
  username: 'local_admin',
  role: 'admin',
  is_active: true,
  created_at: new Date().toISOString(),
};

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [currentUser, setCurrentUser] = useState<UserProfile | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [deploymentMode, setDeploymentMode] = useState<'local' | 'server'>('local');
  const [allowRegistration, setAllowRegistration] = useState<boolean>(false);
  const [systemVersion, setSystemVersion] = useState<string>('1.3.0');

  // Hard logout and clean all state
  const handleClientLogout = useCallback(() => {
    clearAuthToken();
    setCurrentUser(null);
    setIsAuthenticated(false);
  }, []);

  // Initialize identity and authentication on startup
  useEffect(() => {
    let isMounted = true;

    // Register 401 unauthorized handler from Axios client
    setUnauthorizedHandler(() => {
      if (isMounted) {
        handleClientLogout();
      }
    });

    const initAuth = async () => {
      try {
        const modeInfo: AuthModeInfo = await getAuthMode();
        if (!isMounted) return;

        setDeploymentMode(modeInfo.deployment_mode);
        setAllowRegistration(modeInfo.allow_registration);
        setSystemVersion(modeInfo.version || '1.3.0');

        if (modeInfo.deployment_mode === 'local') {
          // Local mode: zero-login bypass with default local identity
          setCurrentUser(LOCAL_DEFAULT_PROFILE);
          setIsAuthenticated(true);
        } else {
          // Server mode: verify existing session token
          const token = getAuthToken();
          if (token) {
            try {
              const user = await getCurrentUser();
              if (isMounted) {
                setCurrentUser(user);
                setIsAuthenticated(true);
              }
            } catch {
              // Token invalid or expired
              if (isMounted) {
                handleClientLogout();
              }
            }
          } else {
            if (isMounted) {
              handleClientLogout();
            }
          }
        }
      } catch (err) {
        console.error('Failed to initialize auth mode:', err);
        // Fallback to local mode in case of discovery failure
        if (isMounted) {
          setDeploymentMode('local');
          setCurrentUser(LOCAL_DEFAULT_PROFILE);
          setIsAuthenticated(true);
        }
      } finally {
        if (isMounted) {
          setIsLoading(false);
        }
      }
    };

    initAuth();

    return () => {
      isMounted = false;
      setUnauthorizedHandler(null);
    };
  }, [handleClientLogout]);

  const login = async (credentials: LoginCredentials): Promise<void> => {
    const res = await loginUser(credentials);
    setAuthToken(res.access_token);
    setCurrentUser(res.user);
    setIsAuthenticated(true);
  };

  const register = async (data: RegisterData): Promise<UserProfile> => {
    return await registerUser(data);
  };

  const logout = async (): Promise<void> => {
    try {
      if (deploymentMode === 'server') {
        await logoutUser();
      }
    } catch {
      // Ignore network errors on logout
    } finally {
      handleClientLogout();
    }
  };

  const refreshCurrentUser = async (): Promise<void> => {
    if (deploymentMode === 'local') {
      setCurrentUser(LOCAL_DEFAULT_PROFILE);
      setIsAuthenticated(true);
      return;
    }
    const user = await getCurrentUser();
    setCurrentUser(user);
    setIsAuthenticated(true);
  };

  const changePassword = async (data: PasswordChangeData): Promise<void> => {
    await changePasswordUser(data);
    // Password change increments token_version on server; invalidate current token and require re-login
    handleClientLogout();
  };

  return (
    <AuthContext.Provider
      value={{
        currentUser,
        isAuthenticated,
        isLoading,
        deploymentMode,
        allowRegistration,
        systemVersion,
        login,
        register,
        logout,
        refreshCurrentUser,
        changePassword,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
