/* Vanilla SVG binding of jeremy-prt/bloub. Geometry, morphs, blink and gaze
   come from the unmodified MIT engine. See LICENSES/Bloub-SOURCE.md. */
import {BotEngine, RAYON, DEMI_VIEWBOX, STATE_BY_ID, mixHex} from '/app/frontend/shared/bloub-engine.js';
const NS='http://www.w3.org/2000/svg';
let nextId=0;
const colorContext=document.createElement('canvas').getContext('2d');
function hexColor(value,fallback){
  colorContext.fillStyle=fallback;
  colorContext.fillStyle=value;
  const color=colorContext.fillStyle;
  if(color.startsWith('#'))return color;
  const channels=color.match(/[\d.]+/g);
  return channels?.length>=3?'#'+channels.slice(0,3).map(n=>Math.round(Number(n)).toString(16).padStart(2,'0')).join(''):fallback;
}
function node(name,attrs={}){const el=document.createElementNS(NS,name);set(el,attrs);return el;}
function set(el,attrs){for(const [key,value] of Object.entries(attrs)) el.setAttribute(key,String(value));}
function pool(parent,values,tag,attrs){
  while(parent.children.length>values.length) parent.lastElementChild.remove();
  values.forEach((value,i)=>{
    const name=typeof tag==='function'?tag(value):tag;
    let el=parent.children[i];
    if(!el||el.localName!==name){const next=node(name);if(el)el.replaceWith(next);else parent.append(next);el=next;}
    set(el,attrs(value,i));
  });
}

// Keep both eyes on one horizontal baseline while preserving the engine's
// blink, perspective scale and gaze translation.
function levelEyes(values){
  if(!values.length)return [];
  const matrices=values.map(eye=>eye.matrix.match(/-?[\d.]+(?:e[+-]?\d+)?/gi).map(Number));
  const baseline=matrices.reduce((sum,matrix)=>sum+matrix[5],0)/matrices.length;
  return values.map((eye,i)=>{
    const [a,b,c,d,x]=matrices[i];
    return {...eye,matrix:`matrix(${Math.hypot(a,c)},0,0,${Math.hypot(b,d)},${x},${baseline})`};
  });
}

export function createBloubAvatar(){
  const id=`desk-bloub-${++nextId}`;
  const svg=node('svg',{viewBox:`${-DEMI_VIEWBOX} ${-DEMI_VIEWBOX} ${DEMI_VIEWBOX*2} ${DEMI_VIEWBOX*2}`,'aria-hidden':'true','data-bloub':'','focusable':'false'});
  const defs=node('defs'),mask=node('mask',{id,maskUnits:'userSpaceOnUse',x:-DEMI_VIEWBOX,y:-DEMI_VIEWBOX,width:DEMI_VIEWBOX*2,height:DEMI_VIEWBOX*2});
  const maskBody=node('path',{fill:'white'}),eyes=node('g',{fill:'black'}),notch=node('circle',{fill:'black'});
  mask.append(maskBody,eyes,notch);defs.append(mask);
  const gradients=node('g');defs.append(gradients);
  const back=node('g',{fill:'none','stroke-linecap':'round'}),rearDots=node('g'),body=node('g'),frontDots=node('g'),front=node('g',{fill:'none','stroke-linecap':'round'}),notif=node('circle');
  const paper=node('path'),ink=node('path',{mask:`url(#${id})`,'data-bloub-body':''});body.append(paper,ink);
  const scene=node('g',{transform:'scale(-1 1)','data-bloub-mirror':''});
  scene.append(back,rearDots,body,frontDots,notif,front);
  svg.append(defs,scene);
  const engine=new BotEngine(RAYON,'idle');
  const motion=matchMedia('(prefers-reduced-motion: reduce)');
  let state='idle',clock=0,last=0,raf=0,visible=true,active=true,destroyed=false;
  const idleShapes=[
    {id:'idle',seconds:4},{id:'egg',seconds:4},{id:'hexagon',seconds:4},
    {id:'play',seconds:3},{id:'wide',seconds:3},{id:'swirl',seconds:3},
    {id:'orbit',seconds:4},{id:'wink',seconds:2}
  ];
  let shapeIndex=0,nextShapeAt=idleShapes[0].seconds;
  let bodyColor='#b20d30',paperColor='#fff';
  const theme=()=>{
    const style=getComputedStyle(svg);
    bodyColor=hexColor(style.getPropertyValue('--color-brand-red').trim()||style.color,'#b20d30');
    paperColor=hexColor(style.getPropertyValue('--color-surface').trim()||style.getPropertyValue('--surface').trim(),'#ffffff');
  };
  function draw(){
    const frame=engine.sample(clock);
    set(maskBody,{d:frame.bodyPath});
    set(ink,{d:frame.bodyPath,fill:bodyColor});set(paper,{d:frame.bodyPath,fill:paperColor});set(body,{opacity:frame.bodyAlpha});
    pool(eyes,levelEyes(frame.eyes),'path',eye=>({d:eye.d,transform:eye.matrix,opacity:eye.alpha}));
    set(notch,frame.notch?{cx:frame.notch.x,cy:frame.notch.y,r:frame.notch.r}:{r:0});
    pool(gradients,frame.arcs,'linearGradient',(arc,i)=>({id:`${id}-arc-${i}`,gradientUnits:'userSpaceOnUse',x1:arc.grad.x1,y1:arc.grad.y1,x2:arc.grad.x2,y2:arc.grad.y2}));
    frame.arcs.forEach((arc,i)=>pool(gradients.children[i],arc.grad.stops,'stop',(color,j)=>({offset:j/(arc.grad.stops.length-1),'stop-color':color})));
    const arcAttrs=side=>(arc,i)=>({d:arc[side],stroke:`url(#${id}-arc-${i})`,'stroke-width':arc.width,opacity:arc.opacity});
    pool(back,frame.arcs,'path',arcAttrs('back'));pool(front,frame.arcs,'path',arcAttrs('front'));
    const dotAttrs=dot=>({fill:dot.color||(dot.depth===undefined?bodyColor:mixHex(paperColor,bodyColor,dot.depth)),opacity:dot.opacity,...(dot.d?{d:dot.d,transform:`translate(${dot.x} ${dot.y}) rotate(${dot.rot||0}) scale(${RAYON})`}:{cx:dot.x,cy:dot.y,r:dot.r})});
    pool(rearDots,frame.dotsBehind?frame.dots:[],d=>d.d?'path':'circle',dotAttrs);
    pool(frontDots,frame.dotsBehind?[]:frame.dots,d=>d.d?'path':'circle',dotAttrs);
    set(notif,frame.notif?{...{cx:frame.notif.x,cy:frame.notif.y,r:frame.notif.r},fill:'#2496e8'}:{r:0});
  }
  function frame(now){raf=0;if(destroyed||!visible||!active||document.hidden)return;clock+=last?Math.min(50,now-last)/1000:0;last=now;
    if(state==='idle'&&clock>=nextShapeAt){
      shapeIndex=(shapeIndex+1)%idleShapes.length;
      const shape=idleShapes[shapeIndex];
      engine.setState(shape.id,clock);
      svg.dataset.bloubState=shape.id;
      nextShapeAt=clock+shape.seconds;
    }
    draw();raf=requestAnimationFrame(frame);}
  function sync(){cancelAnimationFrame(raf);raf=0;last=0;theme();draw();if(!destroyed&&active&&visible&&!motion.matches&&!document.hidden)raf=requestAnimationFrame(frame);}
  function setState(next){
    if(destroyed||!STATE_BY_ID.has(next)||next===state)return;
    state=next;shapeIndex=0;nextShapeAt=clock+idleShapes[0].seconds;
    svg.dataset.bloubState=next;engine.setState(next,clock);
    if(motion.matches)clock+=2;
    sync();
  }
  // Follow the whole workbench, including its same-origin module frames.
  // The engine eases gaze changes; clamp the angle, not the pointer position.
  function lookAt(x,y){
    if(destroyed||motion.matches||!active||!visible||document.hidden)return;
    const bounds=svg.getBoundingClientRect();
    engine.setLook({
      yaw:-35*Math.tanh((x-bounds.left-bounds.width/2)/240),
      pitch:25*Math.tanh(-(y-bounds.top-bounds.height/2)/240),
      mix:1,spin:0,wander:0
    },clock);
  }
  const look=event=>{if(event.pointerType!=='touch')lookAt(event.clientX,event.clientY);};
  const leave=()=>engine.setLook(null,clock);
  const frameListeners=new Map();
  function followFrame(frame){
    frameListeners.get(frame)?.();
    try{
      const target=frame.contentWindow;
      if(!target||!frame.contentDocument)return;
      const move=event=>{
        if(event.pointerType==='touch')return;
        const bounds=frame.getBoundingClientRect();
        lookAt(bounds.left+event.clientX*bounds.width/(frame.clientWidth||bounds.width),
          bounds.top+event.clientY*bounds.height/(frame.clientHeight||bounds.height));
      };
      target.addEventListener('pointermove',move,{passive:true});
      frameListeners.set(frame,()=>target.removeEventListener('pointermove',move));
    }catch{/* Cross-origin frames do not expose pointer events. */}
  }
  const frameObserver=new MutationObserver(records=>{
    if(!records.some(record=>record.removedNodes.length))return;
    for(const [frame,remove] of frameListeners){
      if(!frame.isConnected){remove();frameListeners.delete(frame);}
    }
  });
  frameObserver.observe(document.body,{childList:true,subtree:true});
  const frameLoaded=event=>{if(event.target instanceof HTMLIFrameElement)followFrame(event.target);};
  document.addEventListener('pointermove',look,{passive:true});
  document.documentElement.addEventListener('pointerleave',leave);
  document.addEventListener('load',frameLoaded,true);
  window.addEventListener('blur',leave);
  document.querySelectorAll('iframe').forEach(followFrame);
  const io=new IntersectionObserver(([entry])=>{visible=entry.isIntersecting;sync();});io.observe(svg);
  const observer=new MutationObserver(sync);observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  motion.addEventListener('change',sync);document.addEventListener('visibilitychange',sync);
  svg.dataset.bloubState=state;sync();
  return {element:svg,setState,setActive(value){active=Boolean(value);sync();},destroy(){destroyed=true;cancelAnimationFrame(raf);io.disconnect();observer.disconnect();motion.removeEventListener('change',sync);document.removeEventListener('visibilitychange',sync);document.removeEventListener('pointermove',look);document.documentElement.removeEventListener('pointerleave',leave);document.removeEventListener('load',frameLoaded,true);window.removeEventListener('blur',leave);frameObserver.disconnect();frameListeners.forEach(remove=>remove());frameListeners.clear();}};
}
