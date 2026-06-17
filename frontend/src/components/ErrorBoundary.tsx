import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
  title?: string
}

interface ErrorBoundaryState {
  hasError: boolean
  message: string
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = {
    hasError: false,
    message: '',
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return {
      hasError: true,
      message: error.message || 'unknown render error',
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('frontend boundary caught render error', error, info)
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children
    }

    return (
      <section className="section error-panel">
        <div className="section-header">
          <div>
            <h2 className="section-title">
              {this.props.title || 'Render Fallback'}
            </h2>
            <span className="section-subtitle">
              This block failed to render. The rest of the console stays alive.
            </span>
          </div>
          <span className="section-kicker">degraded ui block</span>
        </div>
        <div className="error-panel-body">
          <strong>{this.state.message}</strong>
          <p>
            Check the browser console for the original stack. This fallback is
            intentional so one visualization cannot blank the whole screen.
          </p>
        </div>
      </section>
    )
  }
}
