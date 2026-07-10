import { FormEvent, useState } from 'react'

const ADMIN_USERNAME = 'admin'
const ADMIN_PASSWORD = 'admin123'

export interface LoginGateProps {
  onAuth: () => void
}

export function LoginGate({ onAuth }: LoginGateProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (username === ADMIN_USERNAME && password === ADMIN_PASSWORD) {
      setError('')
      onAuth()
    } else {
      setError('Invalid username or password.')
    }
  }

  return (
    <div className="flex items-center justify-center p-12 sm:p-6">
      <form 
        className="flex flex-col gap-4 w-full max-w-[360px] p-8 bg-white border border-panel-border rounded-xl shadow-[0_8px_24px_rgba(16,92,120,0.08)]"
        onSubmit={handleSubmit}
      >
        <h2 className="m-0 text-brand-teal text-[1.4rem] text-center font-bold">
          Presets Admin
        </h2>
        <p className="m-0 mb-2 text-[#6b7785] text-[0.9rem] text-center">
          Sign in to manage preset tables.
        </p>

        <label className="flex flex-col gap-[0.35rem]">
          <span className="text-[0.85rem] text-gray-800 font-medium">Username</span>
          <input
            className="px-3 py-[0.55rem] bg-white border border-[#9fc8d8] rounded-lg text-gray-800 text-[0.9rem] outline-none transition-all focus:border-brand-cyan focus:shadow-[0_0_0_2px_rgba(0,173,220,0.25)]"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>

        <label className="flex flex-col gap-[0.35rem]">
          <span className="text-[0.85rem] text-gray-800 font-medium">Password</span>
          <input
            className="px-3 py-[0.55rem] bg-white border border-[#9fc8d8] rounded-lg text-gray-800 text-[0.9rem] outline-none transition-all focus:border-brand-cyan focus:shadow-[0_0_0_2px_rgba(0,173,220,0.25)]"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {error && (
          <div className="text-[#b42318] bg-[#fef3f2] border border-[#fda29b] rounded-lg px-3 py-2 text-[0.85rem]">
            {error}
          </div>
        )}

        <button 
          className="mt-2 px-4 py-[0.6rem] bg-brand-teal text-white border-none rounded-lg text-[0.95rem] font-semibold cursor-pointer transition-colors hover:bg-brand-cyan"
          type="submit"
        >
          Sign In
        </button>
      </form>
    </div>
  )
}
