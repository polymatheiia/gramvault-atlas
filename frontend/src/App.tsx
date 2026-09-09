import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AuthGate } from './components/AuthGate'
import { NavBar } from './components/NavBar'
import { Categorize } from './pages/Categorize'
import { Chat } from './pages/Chat'
import { Digest } from './pages/Digest'
import { Enrich } from './pages/Enrich'
import { Feed } from './pages/Feed'
import { Gallery } from './pages/Gallery'
import { Import } from './pages/Import'
import { ItemDetail } from './pages/ItemDetail'
import { Pull } from './pages/Pull'
import { Settings } from './pages/Settings'

function NotFound() {
  return <p className="mx-auto max-w-3xl px-4 py-12 text-center text-sm text-slate-500">Page not found.</p>
}

function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <div className="min-h-screen bg-surface text-slate-100">
          <NavBar />
          <main>
            <Routes>
              <Route path="/" element={<Gallery />} />
              <Route path="/feed" element={<Feed />} />
              <Route path="/items/:id" element={<ItemDetail />} />
              <Route path="/chat" element={<Chat />} />
              <Route path="/import" element={<Import />} />
              <Route path="/pull" element={<Pull />} />
              <Route path="/enrich" element={<Enrich />} />
              <Route path="/categorize" element={<Categorize />} />
              <Route path="/digest" element={<Digest />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </main>
        </div>
      </AuthGate>
    </BrowserRouter>
  )
}

export default App
