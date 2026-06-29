// Entra ID roles from HAProxy
export type EntraIDRole = 
  | 'ios.operator'      // Standard IOS user
  | 'ios.admin'         // IOS administrator
  | 'skybeam.admin';    // All Skybeam apps admin

export interface AuthData {
  upn: string;              // BNL email (unique identifier)
  name: string;             // User's real name
  roles: EntraIDRole[];     // User's roles
  givenName?: string;       // Optional: first name
  familyName?: string;      // Optional: last name
}

// Declare global window type for auth data
declare global {
  interface Window {
    __AUTH_DATA__?: AuthData;
  }
}
