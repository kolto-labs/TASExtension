import { OriginalGame } from './original-game.js?v=2';
const game = new OriginalGame(document.querySelector('#original-game'));
await game.open();
