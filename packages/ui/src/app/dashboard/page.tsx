'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase/client'
import { User, Session } from '@supabase/supabase-js'
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Progress } from "@/components/ui/progress" // Make sure to install this
import { Skeleton } from "@/components/ui/skeleton" // Make sure to install this
import { 
  Trash2, Copy, Check, Settings, Key, Activity, ShieldCheck, 
  PlusCircle, Settings2, PowerOff, Wallet, AlertTriangle, RefreshCcw,
  Gift, CheckCircle2
} from 'lucide-react'
import { toast } from "sonner"
import { Header } from "@/components/header"
import { ExportCSVButton } from "@/components/export-csv-button"
import { authenticatedFetch, handleApiError as parseApiError, paymentRequiredNotice } from '@/lib/api'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogClose,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Decimal } from 'decimal.js'
import { validateProjectName, validateBudget, sanitizeInput, safeErrorMessage } from '@/lib/utils'
// framer-motion helpers for entrance animations
import { FadeIn, SlideIn, StaggerContainer, StaggerItem, HoverScale } from '@/components/motion-wrapper'
import { BudgetOracle } from '@/components/budget-oracle'
import { CelebrationModal } from '@/components/dashboard/celebration-modal'
import { useAchievements } from '@/hooks/useAchievements'
import { CountUp, AnimatedCurrency } from '@/components/ui/count-up'

// ============================================================================
// TYPE DEFINITIONS (STRICT FINANCIAL PRECISION)
// ============================================================================
// OPTIMIZATION: Backend uses Python Decimal (30-digit precision)
// Frontend uses Decimal.js for all financial arithmetic
// JSON transport uses strings to preserve precision
// This prevents JavaScript's infamous 0.1 + 0.2 = 0.30000000000000004 bug
// 
// The Project interface uses the Decimal type directly
// This provides stronger type safety and eliminates manual string → Decimal conversion
// ============================================================================

type ApiKeyMeta = {
  id: string
  key_prefix: string
  name: string
  created_at: string
}

type Stats = {
    total_requests: number
    threats_blocked: number
}

// ENHANCED: budget_limit and current_spend now use Decimal type for stronger type safety
// Backend sends strings via JSON → Frontend parses to Decimal immediately
// All arithmetic operations use Decimal.js methods (add, sub, mul, div)
type Project = {
  id: string
  name: string
  budget_limit: Decimal  // ← Enhanced: Direct Decimal type (parsed from JSON string)
  current_spend: Decimal // ← Enhanced: Direct Decimal type (parsed from JSON string)
  created_at: string
}

// Helper: Parse API response to Project with Decimal conversion
function parseProjectFromAPI(data: any): Project {
  return {
    id: data.id,
    name: data.name,
    budget_limit: new Decimal(data.budget_limit || '0'),
    current_spend: new Decimal(data.current_spend || '0'),
    created_at: data.created_at
  }
}

// ============================================================================
// DEMO MODE: SAMPLE DATA
// ============================================================================
// Shown when no provider key is connected yet, so the dashboard layout is
// visible before the first real request. All values are static samples.
// ============================================================================

const MOCK_PROJECT: Project = {
  id: 'demo-project-id',
  name: 'Demo Project (sample data)',
  budget_limit: new Decimal('100.00'),
  current_spend: new Decimal('42.00'),
  created_at: new Date().toISOString()
}

const MOCK_STATS: Stats = {
  total_requests: 1247,
  threats_blocked: 12
}

const MOCK_API_KEYS: ApiKeyMeta[] = [
  {
    id: 'demo-key-1',
    key_prefix: 'oxy_demo_',
    name: 'Demo Key (Read-Only)',
    created_at: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(), // 7 days ago
  }
]

export default function DashboardPage() {
  const [user, setUser] = useState<User | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  
  // Loading states
  const [isInitialLoading, setIsInitialLoading] = useState(true)
  const [isMetricsLoading, setIsMetricsLoading] = useState(false)
  const [isGenerating, setIsGenerating] = useState(false)
  
  // Data
  const [projects, setProjects] = useState<Project[]>([])
  const [activeProject, setActiveProject] = useState<Project | null>(null)
  const [existingKeys, setExistingKeys] = useState<ApiKeyMeta[]>([])
  const [stats, setStats] = useState<Stats>({ total_requests: 0, threats_blocked: 0 })
  const [newKey, setNewKey] = useState<string | null>(null)
  const [providerConnected, setProviderConnected] = useState(true)
  
  // Demo mode: sample data until a provider is connected
  const [isDemoMode, setIsDemoMode] = useState(false)

  // UI States
  const [isCopied, setIsCopied] = useState(false)
  const [isProjectModalOpen, setIsProjectModalOpen] = useState(false)
  const [isDeleteProjectModalOpen, setIsDeleteProjectModalOpen] = useState(false)
  
  const [referralLinkCopied, setReferralLinkCopied] = useState(false)
  
  // Referral link comes from the API (not hardcoded)
  const [referralLink, setReferralLink] = useState<string | null>(null)
  const [referralCode, setReferralCode] = useState<string | null>(null)
  
  // Form States
  const [currentProject, setCurrentProject] = useState<Project | null>(null)
  const [projectName, setProjectName] = useState('')
  const [projectBudget, setProjectBudget] = useState('0.00')
  
  // ARIA live region for accessibility
  const [a11yStatus, setA11yStatus] = useState('')

  const router = useRouter()
  const supabase = createClient()

  // 🎉 ACHIEVEMENT SYSTEM: Poll for new achievements and auto-celebrate
  const { newAchievement, clearNewAchievement } = useAchievements(user?.id)

  // --- Helpers ---

  const handleApiError = useCallback(async (res: Response) => {
    // 402 Payment Required: a project that has spent its budget, or (Stripe
    // deployments only) a subscription that needs attention. Say which.
    if (res.status === 402) {
      const body = await res.clone().json().catch(() => null)
      const notice = paymentRequiredNotice(body)
      toast.error(notice.title, {
        description: notice.description,
        ...(notice.billing
          ? { action: { label: 'Update Billing', onClick: () => router.push('/pricing') } }
          : {}),
        duration: 15000, // Show for 15 seconds (important action)
      })
      return notice.billing ? 'Subscription required' : 'Budget spent'
    }

    // 429 is a rate limit: calls came faster than allowed. No plan tier caps
    // requests (since 1.0.1), and a spent budget answers 402, so a 429 is
    // neither of those.
    if (res.status === 429) {
      toast.error('Too many requests', {
        description: 'Calls came faster than the limit allows. Wait a moment and try again.',
        duration: 10000, // Show for 10 seconds
      })
      return 'Rate limited'
    }

    // Handle 403 Forbidden (permissions)
    if (res.status === 403) {
      toast.error('Access Denied', {
        description: 'You do not have permission to perform this action.',
        duration: 5000,
      })
      return 'Permission denied'
    }

    // Handle 401 Unauthorized (session expired)
    if (res.status === 401) {
      toast.error('Session expired. Please log in again.')
      router.push('/login')
      return 'Session expired'
    }

    // Handle 503 Service Unavailable (auth service down)
    if (res.status === 503) {
      toast.error('Service Temporarily Unavailable', {
        description: 'Our systems are initializing. Please try again in 10 seconds.',
        action: {
          label: 'Retry',
          onClick: () => window.location.reload()
        },
        duration: 10000,
      })
      return 'Service unavailable'
    }

    // Handle 500 Internal Server Error
    if (res.status >= 500) {
      toast.error('Server Error', {
        description: 'Our servers are experiencing issues. Please try again later.',
        duration: 7000,
      })
      return 'Server error'
    }

    // Generic error parsing for other status codes
    let errorMsg = 'An unknown error occurred.'
    try {
      const errorData = await res.json()
      // Safely extract error message, prevent XSS
      const detail = errorData.detail
      if (typeof detail === 'string') {
        errorMsg = detail.slice(0, 500) // Limit length
      } else if (Array.isArray(detail) && detail.length > 0) {
        errorMsg = detail[0].msg || 'Validation error'
      } else {
        errorMsg = 'An error occurred.'
      }
    } catch (e) {
      // If JSON parsing fails, try text but limit it
      try {
        const text = await res.text()
        errorMsg = text.slice(0, 500)
      } catch {
        errorMsg = 'An error occurred.'
      }
    }
    // Never return raw user input or system details
    return errorMsg
  }, [router])

  const getSessionToken = useCallback(async () => {
    const { data: { session } } = await supabase.auth.getSession()
    if (!session) {
      toast.error("Session expired. Please log in again.")
      router.push('/login')
      throw new Error("No session")
    }
    return session.access_token
  }, [supabase.auth, router])

  // --- Fetchers ---

  const fetchProjects = useCallback(async () => {
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL
      if (!apiUrl) {
        throw new Error('API URL not configured')
      }
      
      const token = await getSessionToken()
      const res = await fetch(`${apiUrl}/management/projects`, {
         headers: { 'Authorization': `Bearer ${token}` }
      })
      if (!res.ok) throw new Error(await handleApiError(res))

      const data: any[] = await res.json()
      // Parse all projects to convert string → Decimal
      const parsedProjects: Project[] = data.map(parseProjectFromAPI)
      setProjects(parsedProjects)
      
      // Smart selection: keep current if exists, else pick first
      if (data.length > 0) {
        setActiveProject(prev => {
            if (prev && parsedProjects.find(p => p.id === prev.id)) return prev
            return parsedProjects[0]
        })
      }
      return parsedProjects
    } catch (e: unknown) {
      if (process.env.NODE_ENV === 'development') {
        console.error(e)
      }
      toast.error(safeErrorMessage(e))
    }
    return []
  }, [getSessionToken, handleApiError])

  const refreshMetrics = useCallback(async (projectId: string) => {
      setIsMetricsLoading(true)
      try {
          const apiUrl = process.env.NEXT_PUBLIC_API_URL
          if (!apiUrl) {
            throw new Error('API URL not configured')
          }
          
          const token = await getSessionToken()
          // Security: URL encode projectId to prevent injection
          const safeProjectId = encodeURIComponent(projectId)
          const [keysRes, statsRes] = await Promise.all([
            fetch(`${apiUrl}/management/keys?projectId=${safeProjectId}`, {
                headers: { 'Authorization': `Bearer ${token}` }
            }),
            fetch(`${apiUrl}/management/stats?projectId=${safeProjectId}`, {
                headers: { 'Authorization': `Bearer ${token}` }
            })
          ])

          if (keysRes.ok) setExistingKeys(await keysRes.json())
          if (statsRes.ok) setStats(await statsRes.json())
      } catch (e) {
          if (process.env.NODE_ENV === 'development') {
            console.error(e)
          }
      } finally {
          setIsMetricsLoading(false)
      }
  }, [getSessionToken])

  // --- Effects ---

  useEffect(() => {
    const init = async () => {
      const { data: { user }, error } = await supabase.auth.getUser()
      if (error || !user) {
        router.push('/login')
        return
      }
      setUser(user)

      const { data: { session } } = await supabase.auth.getSession()
      if (!session) {
        router.push('/login')
        return
      }
      setSession(session)
      
      try {
        const apiUrl = process.env.NEXT_PUBLIC_API_URL
        if (!apiUrl) {
          if (process.env.NODE_ENV === 'development') {
            console.error("Dashboard: API URL not configured")
          }
          // Graceful fallback - middleware already checked auth
          setProviderConnected(false)
          setIsDemoMode(true)
          setProjects([MOCK_PROJECT])
          setActiveProject(MOCK_PROJECT)
          setStats(MOCK_STATS)
          setExistingKeys(MOCK_API_KEYS)
          setIsInitialLoading(false)
          return
        }
        
        // Retry the status check: up to 3 attempts, 500ms apart
        // Execute up to 3 attempts with 500ms delay between failures
        let statusRes: Response | null = null
        let statusData: any = null
        let lastError: string | null = null
        
        for (let i = 0; i < 3; i++) {
          try {
            statusRes = await fetch(`${apiUrl}/management/status`, {
              headers: { 'Authorization': `Bearer ${session.access_token}` },
              signal: AbortSignal.timeout(10000)
            })
            
            // Success path: Process response and exit retry loop
            if (statusRes.ok) {
              statusData = await statusRes.json()
              if (process.env.NODE_ENV === 'development') {
                console.info(`Dashboard: Status check succeeded on attempt ${i + 1}`)
              }
              break
            }
            
            // Failed response: Log and retry (if not final attempt)
            lastError = `HTTP ${statusRes.status}`
            if (process.env.NODE_ENV === 'development') {
              console.warn(`Dashboard: Status check attempt ${i + 1} failed (${statusRes.status}). ${i < 2 ? 'Retrying in 500ms...' : 'All retries exhausted.'}`)
            }
            
            // Delay before next retry (except on final attempt)
            if (i < 2) {
              await new Promise(resolve => setTimeout(resolve, 500))
            }
            
          } catch (fetchError) {
            // Network error or timeout
            lastError = fetchError instanceof Error ? fetchError.message : 'Network Error'
            if (process.env.NODE_ENV === 'development') {
              console.warn(`Dashboard: Status check attempt ${i + 1} failed (${lastError}). ${i < 2 ? 'Retrying in 500ms...' : 'All retries exhausted.'}`)
            }
            
            // Delay before next retry (except on final attempt)
            if (i < 2) {
              await new Promise(resolve => setTimeout(resolve, 500))
            }
          }
        }
        
        // Fallback after all retries are exhausted
        if (!statusRes || !statusRes.ok || !statusData) {
          if (process.env.NODE_ENV === 'development') {
            console.error(`Dashboard: All 3 status check attempts failed. Last error: ${lastError}`)
          }
          // Middleware already verified auth, default to Demo Mode as safe fallback
          setProviderConnected(false)
          setIsDemoMode(true)
          setProjects([MOCK_PROJECT])
          setActiveProject(MOCK_PROJECT)
          setStats(MOCK_STATS)
          setExistingKeys(MOCK_API_KEYS)
          setIsInitialLoading(false)
          return
        }
        
        // Only redirect if EXPLICITLY not onboarded (defense-in-depth)
        if (statusData.onboarded === false) {
          if (process.env.NODE_ENV === 'development') {
            console.warn("Dashboard: User not onboarded, redirecting...")
          }
          router.push('/onboarding')
          return
        }

        // User is onboarded - check provider status
        if (!statusData.provider_connected) {
          // No provider yet: show demo mode instead of blocking
          if (process.env.NODE_ENV === 'development') {
            console.info("Dashboard: No provider connected, enabling Demo Mode")
          }
          setProviderConnected(false)
          setIsDemoMode(true)
          setProjects([MOCK_PROJECT])
          setActiveProject(MOCK_PROJECT)
          setStats(MOCK_STATS)
          setExistingKeys(MOCK_API_KEYS)
        } else {
          if (process.env.NODE_ENV === 'development') {
            console.info("Dashboard: Provider connected, loading real projects")
          }
          setProviderConnected(true)
          setIsDemoMode(false)
          await fetchProjects()
        }

        // Fetch the referral link from the API (not hardcoded)
        try {
          const referralRes = await fetch(`${apiUrl}/management/credits/referral-link`, {
            headers: { 'Authorization': `Bearer ${session.access_token}` }
          })
          if (referralRes.ok) {
            const referralData = await referralRes.json()
            setReferralCode(referralData.referral_code || null)
            setReferralLink(referralData.referral_link || null)
          }
        } catch (e) {
          // Referral link fetch failed - will show fallback
          if (process.env.NODE_ENV === 'development') {
            console.warn("Dashboard: Failed to fetch referral link", e)
          }
        }
      } catch (e) {
        // Catastrophic error handler
        if (process.env.NODE_ENV === 'development') {
          console.error("Dashboard: Unexpected error in init function", e)
        }
        setProviderConnected(false)
        setIsDemoMode(true)
        setProjects([MOCK_PROJECT])
        setActiveProject(MOCK_PROJECT)
        setStats(MOCK_STATS)
        setExistingKeys(MOCK_API_KEYS)
      } finally {
        setIsInitialLoading(false)
      }
    }
    init()
  }, [router, supabase, fetchProjects])

  // PERFECTIONIST: Token refresh listener for seamless session management
  // Prevents silent token expiry when user stays on dashboard for extended periods
  useEffect(() => {
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, newSession) => {
      if (event === 'TOKEN_REFRESHED' && newSession) {
        // Silently update session without disrupting user
        setSession(newSession)
        if (process.env.NODE_ENV === 'development') {
          console.log('🔄 Token refreshed automatically')
        }
      } else if (event === 'SIGNED_OUT') {
        // User signed out (possibly from another tab)
        router.push('/login')
      }
    })
    
    return () => subscription.unsubscribe()
  }, [supabase.auth, router])

  useEffect(() => {
    if (activeProject && session) {
      setNewKey(null)
      refreshMetrics(activeProject.id)
    }
  }, [activeProject, session, refreshMetrics])

  // --- Actions ---

  const generateKey = async () => {
    // Demo mode: key generation needs a connected provider
    if (isDemoMode) {
      toast.info('Demo Mode: API Keys Disabled', {
        description: 'Connect your OpenAI key to generate real API keys and start protecting your AI.',
        action: {
          label: 'Connect Key →',
          onClick: () => router.push('/settings')
        },
        duration: 7000
      })
      return
    }
    
    if (!user || isGenerating || !activeProject) return
    setIsGenerating(true)
    setA11yStatus('Generating new API key...')
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL
      if (!apiUrl) {
        throw new Error('API URL not configured')
      }
      
      const token = await getSessionToken()
      const safeProjectId = encodeURIComponent(activeProject.id)
      const res = await authenticatedFetch(
        `${apiUrl}/management/key?projectId=${safeProjectId}`,
        { method: 'POST' },
        token
      )
      if (!res.ok) throw new Error(await parseApiError(res))
      
      const data = await res.json()
      setNewKey(data.api_key)
      toast.success('New API key generated')
      setA11yStatus('New API key generated successfully. Copy it now as it will not be shown again.')
      await refreshMetrics(activeProject.id)
    } catch (error: unknown) { 
      const errorMsg = safeErrorMessage(error)
      toast.error(errorMsg)
      setA11yStatus(`Error generating API key: ${errorMsg}`)
    } finally {
        setIsGenerating(false)
    }
  }

  const revokeKey = async (keyId: string) => {
    if (!activeProject) return
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL
      if (!apiUrl) {
        throw new Error('API URL not configured')
      }
      
      const token = await getSessionToken()
      // Security: URL encode IDs to prevent injection
      const safeKeyId = encodeURIComponent(keyId)
      const safeProjectId = encodeURIComponent(activeProject.id)
      const res = await authenticatedFetch(
        `${apiUrl}/management/key/${safeKeyId}?projectId=${safeProjectId}`,
        { method: 'DELETE' },
        token
      )
      if (!res.ok) throw new Error(await parseApiError(res))
      
      setExistingKeys(prev => prev.filter(k => k.id !== keyId))
      toast.success('API key revoked successfully')
    } catch (error: unknown) { 
        toast.error(safeErrorMessage(error))
    }
  }

  const handleProjectSubmit = async () => {
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL
      if (!apiUrl) {
        throw new Error('API URL not configured')
      }
      
      const token = await getSessionToken()
      
      // Enhanced validation with sanitization
      const cleanName = sanitizeInput(projectName.trim())
      const nameValidation = validateProjectName(cleanName)
      if (!nameValidation.valid) {
        toast.error(nameValidation.error || "Invalid project name")
        return
      }
      
      const budgetValidation = validateBudget(projectBudget || '0.0')
      if (!budgetValidation.valid) {
        toast.error(budgetValidation.error || "Invalid budget")
        return
      }
      
      const cleanBudget = new Decimal(String(budgetValidation.value || '0.0'))

      let res: Response
      if (currentProject) {
        // Security: URL encode project ID
        const safeProjectId = encodeURIComponent(currentProject.id)
        res = await authenticatedFetch(
          `${apiUrl}/management/project/${safeProjectId}`,
          {
            method: 'PUT',
            body: JSON.stringify({ name: cleanName, budget_limit: cleanBudget.toString() })
          },
          token
        )
      } else {
        res = await authenticatedFetch(
          `${apiUrl}/management/project`,
          {
            method: 'POST',
            body: JSON.stringify({ name: cleanName })
          },
          token
        )
      }
      
      if (!res.ok) throw new Error(await parseApiError(res))
      
      toast.success(`Project ${currentProject ? 'updated' : 'created'} successfully!`)
      setIsProjectModalOpen(false)
      const newProjects = await fetchProjects()
      
      // Auto-select logic
      if (currentProject) {
          // We edited, keep it active but update data
          const updated = newProjects?.find(p => p.id === currentProject.id)
          if (updated) setActiveProject(updated)
      } else {
          // We created new, select it
          const created = newProjects?.find(p => p.name === cleanName) // Basic heuristic
          if (created) setActiveProject(created)
      }

    } catch (error: unknown) {
      toast.error(safeErrorMessage(error))
    }
  }
  
  const handleDeleteProject = async () => {
    if (!currentProject) return
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL
      if (!apiUrl) {
        throw new Error('API URL not configured')
      }
      
      const token = await getSessionToken()
      // Security: URL encode project ID
      const safeProjectId = encodeURIComponent(currentProject.id)
      const res = await authenticatedFetch(
        `${apiUrl}/management/project/${safeProjectId}`,
        { method: 'DELETE' },
        token
      )
      if (!res.ok) throw new Error(await parseApiError(res))
      
      toast.success(`Project deleted.`)
      setIsDeleteProjectModalOpen(false)
      setCurrentProject(null)
      
      const remaining = await fetchProjects()
      if (remaining.length > 0) setActiveProject(remaining[0])
      else setActiveProject(null)
      
    } catch (error: unknown) {
      toast.error(safeErrorMessage(error))
    }
  }

  const openProjectModal = (project: Project | null) => {
    setCurrentProject(project)
    if (project) {
      setProjectName(project.name)
      // OPTIMIZATION: project.budget_limit is already a string, parse to Decimal for formatting
      // budget_limit is already a Decimal
      setProjectBudget(project.budget_limit.toFixed(2))
    } else {
      setProjectName('')
      setProjectBudget('0.00')
    }
    setIsProjectModalOpen(true)
  }

  const openDeleteProjectModal = (project: Project) => {
    setCurrentProject(project)
    setIsDeleteProjectModalOpen(true)
  }

  // --- UI Components ---

  const renderBudgetCard = () => {
      if (!activeProject) return null
      // OPTIMIZATION: Parse strings to Decimal for precise financial arithmetic
      // This matches backend's Python Decimal implementation exactly
      // No conversion needed: already a Decimal
      const limit = activeProject.budget_limit
      const spend = activeProject.current_spend
      const hasLimit = limit.greaterThan(0)
      
      let percentage = 0
      if (hasLimit) {
          percentage = spend.div(limit).times(100).toNumber()
          if (percentage > 100) percentage = 100
      }

      return (
        <Card className="shadow-sm">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">Budget Usage</CardTitle>
                <Wallet className="h-4 w-4 text-slate-500" />
            </CardHeader>
            <CardContent>
                {/* Animated count-up of the current spend */}
                <div className="text-2xl font-bold flex items-baseline gap-1">
                    <CountUp 
                      value={spend.toNumber()} 
                      prefix="$" 
                      decimals={2}
                      duration={1500}
                    />
                    {hasLimit && (
                      <span className="text-sm font-normal text-slate-500">
                        / ${limit.toFixed(2)}
                      </span>
                    )}
                </div>
                
                {hasLimit ? (
                    <div className="mt-3 space-y-1">
                        <div 
                            className={`h-2 w-full rounded-full overflow-hidden ${percentage > 90 ? "bg-red-100" : "bg-slate-100"}`}
                            role="progressbar"
                            aria-valuenow={percentage}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-label={`Budget usage: ${percentage.toFixed(1)}%`}
                        >
                            <Progress 
                                value={percentage} 
                                className={`h-full ${percentage > 90 ? "[&>div]:bg-red-600" : "[&>div]:bg-slate-900"}`}
                            />
                        </div>
                        <p className="text-xs text-slate-500 text-right" aria-live="polite">
                            {percentage.toFixed(1)}% used
                            {percentage > 90 && (
                                <span className="text-red-600 font-medium ml-2">⚠️ Approaching limit</span>
                            )}
                        </p>
                    </div>
                ) : (
                    <div className="mt-3 space-y-2">
                        <div className="flex items-start gap-2 p-3 bg-amber-50 border border-amber-200 rounded-md">
                            <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0 mt-0.5" />
                            <div className="flex-1">
                                <p className="text-xs font-medium text-amber-900">No Budget Limit Set</p>
                                <p className="text-xs text-amber-700 mt-1">
                                    Unlimited spending enabled. Set a budget to prevent unexpected costs.
                                </p>
                                <Button 
                                    variant="link" 
                                    size="sm" 
                                    className="text-amber-800 hover:text-amber-900 p-0 h-auto mt-1 text-xs"
                                    onClick={() => activeProject && openProjectModal(activeProject)}
                                >
                                    Set Budget Limit →
                                </Button>
                            </div>
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
      )
  }

  const renderProjectSwitcher = () => (
    <div className="flex gap-2 w-full sm:w-auto">
      <Select
        value={activeProject?.id}
        onValueChange={(projectId) => {
          const newActiveProject = projects.find(p => p.id === projectId)
          if (newActiveProject) setActiveProject(newActiveProject)
        }}
        disabled={projects.length === 0}
      >
        <SelectTrigger className="w-full sm:w-[220px] text-left font-medium">
          <SelectValue placeholder="Select Project..." />
        </SelectTrigger>
        <SelectContent>
          {projects.map((project) => (
            <SelectItem key={project.id} value={project.id} className="cursor-pointer">
              {project.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button 
        variant="outline" 
        size="icon" 
        onClick={() => openProjectModal(null)} 
        className="cursor-pointer shrink-0 bg-white hover:bg-slate-50"
        aria-label="Create new project"
        title="Create New Project"
      >
        <PlusCircle className="h-4 w-4" />
        <span className="sr-only">Create New Project</span>
      </Button>
    </div>
  )

  // --- Render ---

  if (isInitialLoading) {
      return (
        <div className="min-h-screen bg-slate-50">
            <Header user={null} title="Control Plane" />
            <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-12 space-y-8">
                <div className="grid gap-4 md:grid-cols-3">
                    <Skeleton className="h-32 w-full rounded-xl" />
                    <Skeleton className="h-32 w-full rounded-xl" />
                    <Skeleton className="h-32 w-full rounded-xl" />
                </div>
                <Skeleton className="h-64 w-full rounded-xl" />
            </main>
        </div>
      )
  }

  if (!user) return null

  return (
    <div className="min-h-screen bg-slate-50">
      <Header user={user} title="Control Plane">
        <div className="hidden sm:flex items-center gap-4">
          {renderProjectSwitcher()}
          <Button 
            variant="ghost" 
            size="sm" 
            onClick={() => router.push('/settings')} 
            className="cursor-pointer text-slate-600"
            aria-label="Open settings"
            title="Settings"
          >
            <Settings className="h-4 w-4" />
            <span className="sr-only">Settings</span>
          </Button>
        </div>
      </Header>

      {/* ARIA Live Region for screen readers */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {a11yStatus}
      </div>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-12 space-y-8">
        
        <div className="flex sm:hidden mb-6">
          {renderProjectSwitcher()}
        </div>
        
        {/* Demo mode banner */}
        {isDemoMode && (
          <Card className="shadow-lg border-2 border-purple-300 bg-linear-to-r from-purple-50 to-blue-50 mb-8">
            <CardContent className="p-6">
              <div className="flex items-start gap-4">
                <div className="p-3 bg-linear-to-br from-purple-600 to-blue-600 rounded-xl shadow-lg">
                  <Activity className="h-6 w-6 text-white" />
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-3 mb-2">
                    <h3 className="text-xl font-bold text-slate-900">
                      👋 Welcome to Demo Mode
                    </h3>
                    <Badge className="bg-purple-100 text-purple-700 border-purple-300">
                      Sample data
                    </Badge>
                  </div>
                  <p className="text-slate-700 mb-4 text-base">
                    You're viewing a <strong>demo dashboard</strong> with sample data. 
                    Connect a provider key to route real traffic through the gateway.
                  </p>
                  <div className="flex flex-wrap gap-3">
                    <Button 
                      onClick={() => router.push('/settings')} 
                      size="lg"
                      className="bg-linear-to-r from-purple-600 to-blue-600 hover:from-purple-700 hover:to-blue-700 text-white shadow-md"
                    >
                      <Key className="mr-2 h-5 w-5" />
                      Connect a Provider Key
                    </Button>
                    <Button 
                      variant="outline" 
                      size="lg"
                      onClick={() => {
                        toast.info('Exploring Demo Mode', {
                          description: 'All data shown is simulated. Connect your API key to see real metrics.',
                          duration: 5000
                        })
                      }}
                    >
                      Continue Exploring Demo
                    </Button>
                  </div>
                  <div className="mt-4 pt-4 border-t border-purple-200">
                    <p className="text-sm text-slate-600 flex items-center gap-2">
                      <CheckCircle2 className="h-4 w-4 text-green-600" />
                      Add a provider key in Settings, or point Oxy at a local model (Ollama, vLLM)
                    </p>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        )}
        
        {!activeProject && !isDemoMode ? (
            <div className="text-center py-20">
                <div className="bg-white p-8 rounded-2xl shadow-sm border inline-block max-w-md w-full">
                    <div className="h-12 w-12 bg-blue-50 rounded-full flex items-center justify-center mx-auto mb-4">
                        <PlusCircle className="h-6 w-6 text-blue-600" />
                    </div>
                    <h3 className="text-lg font-semibold text-slate-900">Create your first project</h3>
                    <p className="text-slate-500 mt-2 mb-6">Projects hold your API keys and budget limits. Create one to get started.</p>
                    <Button onClick={() => openProjectModal(null)} className="w-full cursor-pointer">
                        Create Project
                    </Button>
                </div>
            </div>
        ) : (
          <>
            {/* Metrics Row */}
            <div className="grid gap-4 md:grid-cols-3">
                {renderBudgetCard()}
                
                {/* Total requests (animated count-up) */}
                <Card className={`shadow-sm ${isDemoMode ? 'border-purple-200 bg-purple-50/30' : ''}`}>
                  <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-medium">Total Requests</CardTitle>
                    <Activity className={`h-4 w-4 ${isDemoMode ? 'text-purple-500' : 'text-slate-500'}`} />
                  </CardHeader>
                  <CardContent>
                    {isMetricsLoading ? (
                        <Skeleton className="h-8 w-20" />
                    ) : (
                        <>
                          <div className="text-2xl font-bold">
                            <CountUp 
                              value={stats.total_requests} 
                              duration={2000}
                              separator=","
                            />
                          </div>
                          {isDemoMode && (
                            <Badge className="mt-2 text-xs bg-purple-100 text-purple-700 border-purple-300">
                              Demo Data
                            </Badge>
                          )}
                        </>
                    )}
                    <p className="text-xs text-slate-500 mt-2">Lifetime volume</p>
                  </CardContent>
                </Card>

                {/* Threats blocked (animated count-up) */}
                <Card className={`shadow-sm ${isDemoMode ? 'border-purple-200 bg-purple-50/30' : ''}`}>
                  <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-medium">Threats Blocked</CardTitle>
                    <ShieldCheck className={`h-4 w-4 ${isDemoMode ? 'text-purple-500' : 'text-green-600'}`} />
                  </CardHeader>
                  <CardContent>
                    {isMetricsLoading ? (
                        <Skeleton className="h-8 w-20" />
                    ) : (
                        <>
                          <div className={`text-2xl font-bold ${isDemoMode ? 'text-purple-700' : 'text-green-700'}`}>
                            <CountUp 
                              value={stats.threats_blocked} 
                              duration={2500}
                              separator=","
                            />
                          </div>
                          {isDemoMode && (
                            <Badge className="mt-2 text-xs bg-purple-100 text-purple-700 border-purple-300">
                              Demo Data
                            </Badge>
                          )}
                        </>
                    )}
                    <p className="text-xs text-slate-500 mt-2">Auto-mitigated</p>
                  </CardContent>
                </Card>
            </div>


            {/* Management Area */}
            <div className="space-y-6">
                <div className="flex items-center justify-between flex-wrap gap-4">
                    <div>
                        <h2 className="text-lg font-semibold text-slate-900">API Keys</h2>
                        <p className="text-sm text-slate-500">Manage access for {activeProject?.name || 'your project'}</p>
                    </div>
                    <div className="flex gap-2 flex-wrap">
                        {/* CSV Export (ghost variant, icon size) */}
                        {!isDemoMode && <ExportCSVButton />}
                        <Button 
                            variant="ghost" 
                            size="sm" 
                            onClick={() => activeProject && refreshMetrics(activeProject.id)}
                            disabled={isMetricsLoading}
                            className="cursor-pointer"
                            title="Refresh metrics"
                            aria-label="Refresh metrics and API keys"
                        >
                            <RefreshCcw className={`h-4 w-4 ${isMetricsLoading ? 'animate-spin' : ''}`} />
                            <span className="ml-2 hidden sm:inline">Refresh</span>
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => openProjectModal(activeProject)} className="cursor-pointer">
                            <Settings2 className="h-4 w-4 mr-2" />
                            Settings
                        </Button>
                        <Button 
                            variant="outline" 
                            size="sm" 
                            onClick={() => activeProject && openDeleteProjectModal(activeProject)} 
                            disabled={!activeProject}
                            className="cursor-pointer text-red-600 hover:bg-red-50 border-red-200 disabled:opacity-50"
                        >
                            <Trash2 className="h-4 w-4 mr-2" />
                            Delete
                        </Button>
                    </div>
                </div>

                <Card className="shadow-sm overflow-hidden">
                    {newKey && (
                        <div className="bg-emerald-50 p-6 border-b border-emerald-100 animate-in slide-in-from-top-2">
                            <div className="flex items-start gap-4">
                                <div className="p-2 bg-emerald-100 rounded-full shrink-0">
                                    <Check className="h-5 w-5 text-emerald-700" />
                                </div>
                                <div className="space-y-4 w-full">
                                    <div>
                                        <h4 className="text-emerald-900 font-semibold">API Key Generated!</h4>
                                        <p className="text-emerald-700 text-sm">Copy it now — this key won't be shown again.</p>
                                    </div>
                                    <div className="flex gap-2 max-w-xl">
                                        <div className="relative w-full">
                                            <Input 
                                                className="font-mono text-sm pr-24 bg-white border-emerald-300 focus-visible:ring-emerald-500" 
                                                value={newKey} 
                                                readOnly
                                                aria-label="Generated API key"
                                                aria-describedby="new-key-instructions"
                                            />
                                            <p id="new-key-instructions" className="sr-only">
                                                This is your new API key. Copy it now as it will not be shown again.
                                            </p>
                                        </div>
                                        <Button 
                                            onClick={() => {
                                                navigator.clipboard.writeText(newKey)
                                                setIsCopied(true)
                                                toast.success('Copied to clipboard')
                                                setTimeout(() => setIsCopied(false), 2000)
                                            }} 
                                            className="cursor-pointer shrink-0 bg-emerald-600 hover:bg-emerald-700"
                                            aria-label={isCopied ? "Copied to clipboard" : "Copy API key to clipboard"}
                                            title={isCopied ? "Copied!" : "Copy to clipboard"}
                                        >
                                            {isCopied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                                        </Button>
                                    </div>
                                    
                                    {/* Usage Instructions */}
                                    <div className="bg-white/80 rounded-xl p-4 border border-emerald-200 mt-4">
                                        <h5 className="text-sm font-semibold text-slate-900 mb-3 flex items-center gap-2">
                                            <span>📋</span> How to use your key
                                        </h5>
                                        <div className="space-y-3 text-sm">
                                            <div className="bg-slate-900 rounded-lg p-3 font-mono text-xs text-slate-100 overflow-x-auto">
                                                <div className="text-slate-500"># Python (OpenAI SDK)</div>
                                                <div><span className="text-violet-400">import</span> openai</div>
                                                <div className="mt-2">openai.api_key = <span className="text-emerald-400">"{newKey.slice(0, 20)}..."</span></div>
                                                <div>openai.api_base = <span className="text-emerald-400">"http://localhost:8000/v1"</span></div>
                                            </div>
                                            <p className="text-slate-600 text-xs">
                                                Works with OpenAI SDK, LangChain, and any OpenAI-compatible library. 
                                                Just change your API key and base URL.
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    )}

                    <div className="p-0">
                        {isMetricsLoading ? (
                            <div className="p-6 space-y-3">
                                {/* Realistic table skeletons */}
                                {[1, 2, 3].map((i) => (
                                    <div key={i} className="flex items-center gap-4 p-3 border-b border-slate-100 last:border-0">
                                        <Skeleton className="h-5 w-40" /> {/* Key Name */}
                                        <Skeleton className="h-6 w-24 rounded-full" /> {/* Prefix badge */}
                                        <Skeleton className="h-5 w-32 ml-auto" /> {/* Date */}
                                        <Skeleton className="h-8 w-8 rounded" /> {/* Action button */}
                                    </div>
                                ))}
                            </div>
                        ) : existingKeys.length > 0 ? (
                            <Table>
                                <TableHeader className="bg-slate-50/50">
                                    <TableRow>
                                        <TableHead className="pl-6">Key Name</TableHead>
                                        <TableHead>Prefix</TableHead>
                                        <TableHead>Created</TableHead>
                                        <TableHead className="w-[50px]"></TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {existingKeys.map((key) => (
                                        <TableRow key={key.id} className="group">
                                            <TableCell className="pl-6 font-medium text-slate-900">
                                                {key.name}
                                            </TableCell>
                                            <TableCell>
                                                <code className="bg-slate-100 px-2 py-1 rounded text-xs font-mono text-slate-600">
                                                    {key.key_prefix}...
                                                </code>
                                            </TableCell>
                                            <TableCell className="text-slate-500 text-sm">
                                                {new Date(key.created_at).toLocaleDateString(undefined, { 
                                                    year: 'numeric', month: 'short', day: 'numeric' 
                                                })}
                                            </TableCell>
                                            <TableCell>
                                                <AlertDialog>
                                                    <AlertDialogTrigger asChild>
                                                        <Button 
                                                            variant="ghost" 
                                                            size="icon"
                                                            className="cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity text-slate-500 hover:text-red-600 hover:bg-red-50"
                                                            aria-label={`Revoke API key ${key.name}`}
                                                            title={`Revoke ${key.name}`}
                                                        >
                                                            <Trash2 className="h-4 w-4" />
                                                        </Button>
                                                    </AlertDialogTrigger>
                                                    <AlertDialogContent>
                                                        <AlertDialogHeader>
                                                            <AlertDialogTitle>Revoke Key "{key.name}"?</AlertDialogTitle>
                                                            <AlertDialogDescription>
                                                                Any applications using this key ({key.key_prefix}...) will fail immediately.
                                                            </AlertDialogDescription>
                                                        </AlertDialogHeader>
                                                        <AlertDialogFooter>
                                                            <AlertDialogCancel className="cursor-pointer">Cancel</AlertDialogCancel>
                                                            <AlertDialogAction 
                                                                onClick={() => revokeKey(key.id)}
                                                                className="cursor-pointer bg-red-600 hover:bg-red-700 focus:ring-red-600"
                                                            >
                                                                Revoke
                                                            </AlertDialogAction>
                                                        </AlertDialogFooter>
                                                    </AlertDialogContent>
                                                </AlertDialog>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        ) : (
                            <div className="text-center py-16 px-4">
                                <div className="bg-slate-50 h-16 w-16 rounded-full flex items-center justify-center mx-auto mb-4">
                                    <Key className="h-8 w-8 text-slate-300" />
                                </div>
                                <h3 className="text-sm font-semibold text-slate-900">No active keys</h3>
                                <p className="mt-1 text-sm text-slate-500 max-w-xs mx-auto mb-6">
                                    Generate an API key to start sending requests through the engine.
                                </p>
                                <Button 
                                    onClick={generateKey} 
                                    disabled={isGenerating} 
                                    className="cursor-pointer"
                                    aria-label="Generate your first API key"
                                >
                                    {isGenerating ? (
                                        <>
                                            <RefreshCcw className="h-4 w-4 mr-2 animate-spin" />
                                            Generating...
                                        </>
                                    ) : (
                                        <>
                                            <PlusCircle className="h-4 w-4 mr-2" />
                                            Generate First Key
                                        </>
                                    )}
                                </Button>
                            </div>
                        )}
                    </div>
                    
                    {existingKeys.length > 0 && (
                        <div className="bg-slate-50/50 border-t border-slate-100 p-4 flex justify-end">
                            <Button 
                                onClick={generateKey} 
                                disabled={isGenerating} 
                                className="cursor-pointer"
                                aria-label={isGenerating ? "Generating API key..." : "Generate new API key"}
                            >
                                {isGenerating ? (
                                    <>
                                        <RefreshCcw className="h-4 w-4 mr-2 animate-spin" aria-hidden="true" />
                                        Generating...
                                    </>
                                ) : (
                                    <>
                                        <PlusCircle className="h-4 w-4 mr-2" aria-hidden="true" />
                                        Generate Key
                                    </>
                                )}
                            </Button>
                        </div>
                    )}
                </Card>
                
                {/* Referral widget: referral credits (amount set by the operator) */}
                {!isDemoMode && (
                  <Card className="mt-6 bg-linear-to-br from-green-50 to-emerald-50 border-2 border-green-200 shadow-lg">
                    <CardContent className="p-6">
                      <div className="flex items-start gap-4">
                        <div className="p-3 bg-linear-to-br from-green-600 to-emerald-600 rounded-xl shadow-md flex-shrink-0">
                          <Gift className="h-6 w-6 text-white" />
                        </div>
                        <div className="flex-1">
                          <h3 className="text-xl font-bold text-green-900 mb-2">
                            Referral credits
                          </h3>
                          <p className="text-green-800 mb-4 text-sm">
                            Share your referral link. When a referred org signs up, both orgs receive a usage credit
                            (the amount is set by the operator of this deployment).
                          </p>
                          
                          <div className="flex gap-2 mb-3">
                            <Input
                              value={referralLink || 'Loading...'}
                              readOnly
                              className="flex-1 bg-white border-green-300 text-sm font-mono"
                              aria-label="Your referral link"
                            />
                            <Button
                              onClick={() => {
                                if (referralLink) {
                                  navigator.clipboard.writeText(referralLink)
                                  setReferralLinkCopied(true)
                                  toast.success('Referral link copied!', {
                                    description: `Share it to earn a referral credit.${referralCode ? ` Code: ${referralCode}` : ''}`,
                                    duration: 4000
                                  })
                                  setTimeout(() => setReferralLinkCopied(false), 2000)
                                }
                              }}
                              className="bg-green-600 hover:bg-green-700 text-white"
                              disabled={!referralLink}
                            >
                              {referralLinkCopied ? (
                                <>
                                  <Check className="h-4 w-4 mr-2" />
                                  Copied!
                                </>
                              ) : (
                                <>
                                  <Copy className="h-4 w-4 mr-2" />
                                  Copy Link
                                </>
                              )}
                            </Button>
                          </div>
                          
                          <div className="bg-white/60 backdrop-blur-sm rounded-lg p-3 border border-green-200">
                            <p className="text-xs text-green-800">
                              <strong>How it works:</strong> share your link → a new org signs up with it → both orgs receive a usage credit, up to the per-referrer cap the operator sets.
                            </p>
                          </div>
                        </div>
                      </div>
                    </CardContent>
                  </Card>
                )}
            </div>
          </>
        )}
      </main>

      {/* --- Modals --- */}
      <Dialog open={isProjectModalOpen} onOpenChange={setIsProjectModalOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{currentProject ? 'Project Settings' : 'New Project'}</DialogTitle>
            <DialogDescription>
              Configure identification and limits for this project.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-6 py-4">
            <div className="space-y-2">
              <Label htmlFor="project-name">Project Name</Label>
              <Input
                id="project-name"
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                placeholder="e.g. Mobile App Prod"
                aria-describedby="project-name-help"
                maxLength={100}
              />
              <p id="project-name-help" className="text-xs text-slate-500 mt-1">
                A descriptive name to identify this project (2-100 characters)
              </p>
            </div>
            {/* Budget Oracle for NEW projects */}
            {!currentProject && user?.email ? (
              <BudgetOracle 
                userEmail={user.email}
                onBudgetSelect={(budget) => {
                  setProjectBudget(budget.toString())
                }}
              />
            ) : (
              /* Traditional input for EDITING existing projects */
              <div className="space-y-2">
                <Label htmlFor="project-budget">Budget limit, total spend ($)</Label>
                <div className="relative">
                    <span className="absolute left-3 top-2.5 text-slate-500 pointer-events-none" aria-hidden="true">$</span>
                    <Input
                      id="project-budget"
                      type="number"
                      value={projectBudget}
                      onChange={(e) => setProjectBudget(e.target.value)}
                      className="pl-7"
                      min="0.00"
                      step="0.01"
                      placeholder="0.00"
                      aria-describedby="project-budget-help"
                    />
                </div>
                <p id="project-budget-help" className="text-xs text-slate-500">
                  The most this project can spend in total: nothing resets it. Use 0.00 for no cap. Once it is spent, requests are refused with 402 until you raise it.
                </p>
              </div>
            )}
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline" className="cursor-pointer">Cancel</Button>
            </DialogClose>
            <Button onClick={handleProjectSubmit} className="cursor-pointer">
              {currentProject ? 'Save Changes' : 'Create Project'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      
      <AlertDialog open={isDeleteProjectModalOpen} onOpenChange={setIsDeleteProjectModalOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="text-red-600">Delete {currentProject?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently delete the project and <strong>immediately revoke all API keys</strong> associated with it. 
              Requests using these keys will fail. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="cursor-pointer">Cancel</AlertDialogCancel>
            <AlertDialogAction 
              onClick={handleDeleteProject}
              className="cursor-pointer bg-red-600 hover:bg-red-700 focus:ring-red-600"
            >
              Delete Project
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 🎉 CELEBRATION MODAL: Auto-triggered when achievement detected */}
      {newAchievement && (
        <CelebrationModal 
          achievement={{
            type: newAchievement.achievement_type,
            data: newAchievement.achievement_data
          }}
          onClose={clearNewAchievement}
        />
      )}

    </div>
  )
}