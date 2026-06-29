import { createContext, useContext, type ReactNode } from 'react';
import type { AuthData, EntraIDRole } from '../types/auth';

interface AuthContextValue extends AuthData {
  hasRole: (role: EntraIDRole) => boolean;
  isAdmin: () => boolean;
  canAccessPresetsAdmin: () => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

interface AuthProviderProps {
  authData: AuthData | null;
  children: ReactNode;
}

export function AuthProvider({ authData, children }: AuthProviderProps) {
  if (!authData) {
    // No auth data - render error or redirect
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <h1>Authentication Required</h1>
        <p>No authentication headers received from HAProxy.</p>
        <p>This application requires Entra ID authentication.</p>
      </div>
    );
  }

  const hasRole = (role: EntraIDRole): boolean => {
    return authData.roles.includes(role);
  };

  const isAdmin = (): boolean => {
    // skybeam.admin has all permissions, including ios.admin
    return hasRole('ios.admin') || hasRole('skybeam.admin');
  };

  const canAccessPresetsAdmin = (): boolean => {
    return isAdmin();
  };

  const value: AuthContextValue = {
    ...authData,
    hasRole,
    isAdmin,
    canAccessPresetsAdmin,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
