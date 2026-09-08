import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Landing } from './routes/Landing';
import { Room } from './routes/Room';
import './ui/styles.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/room/:roomCode" element={<Room />} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
);
