// Entry point of the LangStage panel's webview bundle (esbuild → dist/webview/main.js).
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles.css';

const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
