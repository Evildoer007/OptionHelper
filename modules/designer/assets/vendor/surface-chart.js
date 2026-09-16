/* Frozen report grids: presentation only, no interpolation or pricing. */
(() => {
  'use strict';
  function option(spec, formatValue = value => String(value), ink = '#555') {
    const x = spec.x || [], y = spec.y || [];
    const numeric = values => values.every(v => v !== null && v !== '' && Number.isFinite(Number(v)));
    const nx = numeric(x), ny = numeric(y);
    const cells = new Map((spec.data || []).map(row => [`${row[0]}:${row[1]}`, row[2]]));
    const data = y.flatMap((yv, j) => x.map((xv, i) => {
      const z = cells.get(`${i}:${j}`);
      return [nx ? Number(xv) : i, ny ? Number(yv) : j, z === null || z === undefined ? NaN : Number(z)];
    }));
    const z = data.map(row => row[2]).filter(Number.isFinite);
    const axis = (values, name, isNumeric) => ({type: isNumeric ? 'value' : 'category', data: isNumeric ? undefined : values,
      name: name || '', scale: true, nameTextStyle:{color:ink}, axisLabel:{color:ink}, axisLine:{lineStyle:{color:'#aaa'}}});
    return {
      animation:false, backgroundColor:'transparent',
      tooltip:{confine:true, formatter:item => {
        const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        const row=item.value;
        return `${escape(spec.x_axis_name)}：${escape(nx?row[0]:x[row[0]])}<br>${escape(spec.y_axis_name)}：${escape(ny?row[1]:y[row[1]])}<br>${escape(spec.z_axis_name)}：${escape(formatValue(row[2]))}`;
      }},
      xAxis3D:axis(x,spec.x_axis_name,nx), yAxis3D:axis(y,spec.y_axis_name,ny),
      zAxis3D:{type:'value',name:spec.z_axis_name||'',scale:true,axisLabel:{color:ink,formatter:formatValue},nameTextStyle:{color:ink}},
      grid3D:{boxWidth:110,boxDepth:85,boxHeight:65,top:0,bottom:35,
        viewControl:{alpha:25,beta:35,distance:210,autoRotate:false},light:{main:{intensity:1.1},ambient:{intensity:.6}}},
      visualMap:{min:z.length?Math.min(...z):0,max:z.length?Math.max(...z):1,dimension:2,show:true,
        orient:'horizontal',left:'center',bottom:0,formatter:formatValue,textStyle:{color:ink},inRange:{color:['#3478b8','#f3eeee','#c8102e']}},
      series:[{type:x.length>1 && y.length>1 ? 'surface' : 'scatter3D',symbolSize:6,shading:'color',wireframe:{show:true,lineStyle:{color:'rgba(80,80,80,.18)',width:.5}},data}]
    };
  }
  window.OptionHelperSurface = Object.freeze({option});
})();
